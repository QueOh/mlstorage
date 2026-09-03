"""
Hierarchical prefix caching for KV Cache Benchmark.

Models the reuse of common prompts (e.g., system prompts) across
users to reduce redundant cache allocations.
"""

import hashlib
import random
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple
from datetime import datetime
from enum import Enum

from kv_cache.config import cfg
from kv_cache.models import ModelConfig, InferenceRequest


class PrefixType(Enum):
    """Enumeration for the different tiers of prefix caching."""
    SYSTEM_PROMPT = "system_prompt"
    COMMON_PHRASE = "common_phrase"
    USER_SPECIFIC = "user_specific"


@dataclass
class PrefixCacheEntry:
    """Represents a cached prefix."""
    prefix_key: str
    prefix_type: PrefixType
    text_hash: str
    token_count: int
    kv_cache_key: str

    # Usage statistics to track popularity and reuse.
    use_count: int = 0
    first_seen: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    users_using: Set[str] = field(default_factory=set)

    # Storage information.
    storage_tier: str = ""
    size_bytes: int = 0


class PrefixMatcher:
    """Detects and matches common prefixes in requests to enable reuse."""

    COMMON_SYSTEM_PROMPTS = [
        "You are a helpful assistant.",
        "You are an AI assistant helping with coding tasks.",
        "You are a professional writing assistant.",
    ]

    def __init__(self, min_prefix_length: int = None):
        self.min_prefix_length = min_prefix_length if min_prefix_length is not None else cfg('prefix_cache', 'min_prefix_length', default=50)
        self.prefix_index: Dict[str, PrefixCacheEntry] = {}
        self.prefix_frequency: Dict[str, int] = {}
        self.lock = threading.Lock()

    def hash_prefix(self, text: str, token_count: int) -> str:
        """Creates a deterministic hash for a given text prefix."""
        content = f"{text[:500]}_{token_count}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def detect_system_prompt(self, context_tokens: int) -> Optional[PrefixCacheEntry]:
        """LEGACY mode: a coin flip, not content matching. Retained only
        for continuity cells (offload-stages s6=off); never a measured
        arm. See detect_system_prompt_real for the real index."""
        system_prompt_hit_probability = cfg('prefix_cache', 'system_prompt_hit_probability', default=0.2)
        if random.random() < system_prompt_hit_probability:
            system_prompt = random.choice(self.COMMON_SYSTEM_PROMPTS)
            prefix_hash = self.hash_prefix(system_prompt, len(system_prompt.split()))

            with self.lock:
                if prefix_hash in self.prefix_index:
                    entry = self.prefix_index[prefix_hash]
                    entry.use_count += 1
                    entry.last_used = datetime.now()
                    return entry
                else:
                    entry = PrefixCacheEntry(
                        prefix_key=f"system_{prefix_hash}",
                        prefix_type=PrefixType.SYSTEM_PROMPT,
                        text_hash=prefix_hash,
                        token_count=len(system_prompt.split()),
                        kv_cache_key=f"kv_system_{prefix_hash}",
                        use_count=1
                    )
                    self.prefix_index[prefix_hash] = entry
                    return entry
        return None

    # ---- REAL index (09-03, S6 host arm) ---------------------------------
    #
    # Content-derived matching instead of RNG: every user deterministically
    # carries ONE system prompt (seeded by user_id), whose token blocks are
    # hashed into a block-chain. A request hits iff its user's prompt chain
    # was REGISTERED by an earlier request -- same-content reuse, zero
    # randomness. Scope is deliberately system-prompt-only: multi-turn
    # history reuse is already modeled (and measured) by the multi_turn
    # stage, and double-modeling it here would double-count savings.
    #
    # The index record layout mirrors what the device-side kv_prefix_lookup
    # (PIND 11) scan will consume in the device arm: fixed 32-byte records
    # {prefix_hash: u64, depth: u32, token_count: u32, key_handle: u64,
    #  crc: u32, pad: u32} -- publish_records() emits exactly that.

    PREFIX_BLOCK_TOKENS = 128

    def _user_prompt_id(self, user_id: str) -> int:
        h = hashlib.sha256(f"sysprompt_{user_id}".encode()).digest()
        return int.from_bytes(h[:4], "little") % len(self.COMMON_SYSTEM_PROMPTS)

    def _prompt_chain(self, prompt_id: int, sys_tokens: int) -> list:
        """Block-chain hashes for one system prompt (u64 per block)."""
        n_blocks = max(1, (sys_tokens + self.PREFIX_BLOCK_TOKENS - 1)
                       // self.PREFIX_BLOCK_TOKENS)
        chain = []
        prev = b"prefix_root"
        for b in range(n_blocks):
            digest = hashlib.sha256(
                prev + f"sys{prompt_id}_blk{b}".encode()).digest()
            chain.append(int.from_bytes(digest[:8], "little"))
            prev = digest
        return chain

    def detect_system_prompt_real(self, user_id: str) -> Optional[PrefixCacheEntry]:
        """Real lookup: longest-match of the user's prompt block-chain
        against the registered index; register on first sight (the miss
        that seeds later hits -- exactly how prefix caches behave)."""
        sys_tokens = int(cfg('prefix_cache', 'system_prompt_tokens', default=128))
        prompt_id = self._user_prompt_id(user_id)
        chain = self._prompt_chain(prompt_id, sys_tokens)

        with self.lock:
            # longest match first (deepest chain hash)
            for depth in range(len(chain) - 1, -1, -1):
                key = f"blk_{chain[depth]:016x}"
                entry = self.prefix_index.get(key)
                if entry is not None:
                    entry.use_count += 1
                    entry.last_used = datetime.now()
                    return entry
            # miss: register the full chain's deepest node
            key = f"blk_{chain[-1]:016x}"
            entry = PrefixCacheEntry(
                prefix_key=f"system_{prompt_id}",
                prefix_type=PrefixType.SYSTEM_PROMPT,
                text_hash=f"{chain[-1]:016x}",
                token_count=min(sys_tokens,
                                len(chain) * self.PREFIX_BLOCK_TOKENS),
                kv_cache_key=f"kv_system_p{prompt_id}",
                use_count=0)
            self.prefix_index[key] = entry
            return None

    def publish_records(self) -> bytes:
        """The 32-byte-record index image the device arm scans (P3);
        also the host arm's ground truth for host==device parity gates."""
        import struct
        import zlib
        recs = []
        with self.lock:
            for key, e in sorted(self.prefix_index.items()):
                if not key.startswith("blk_"):
                    continue
                h64 = int(key[4:], 16)
                body = struct.pack("<QII Q", h64, 0, e.token_count,
                                  zlib.crc32(e.kv_cache_key.encode()))
                crc = zlib.crc32(body) & 0xFFFFFFFF
                recs.append(body + struct.pack("<II", crc, 0))
        return b"".join(recs)


class PrefixCacheManager:
    """Orchestrates the prefix matching and caching logic."""

    def __init__(self, cache, max_prefix_entries: int = None,
                 mode: str = 'off'):
        # offload-stages s6: 'off' = legacy coin flip (continuity cells
        # only), 'host' = real block-chain index, 'device' lands with P3
        # (PIND 11 scan; until then it looks up host-side).
        self.mode = str(mode or 'off')
        self.cache = cache
        self.max_prefix_entries = max_prefix_entries if max_prefix_entries is not None else cfg('prefix_cache', 'max_prefix_entries', default=1000)
        self.prefix_matcher = PrefixMatcher()
        self.lock = threading.Lock()

        self.stats = {
            'prefix_hits': 0,
            'prefix_misses': 0,
            'system_prompt_reuse': 0,
            'common_phrase_reuse': 0,
            'bytes_saved': 0,
            'device_lookups': 0,
            'device_parity_mismatches': 0,
            'device_lookup_errors': 0,
        }
        # device-arm publish state: re-publish the 32B-record image only
        # when the index grew (register-on-miss marks it dirty).
        self._published_index_size = -1

    def _device_lookup_parity(self, request, host_entry) -> None:
        """S6 device arm (P3): run the SAME lookup on the device (PIND 11
        scan over the published index image) and count parity against the
        host result. Errors fall back silently to the host result but are
        counted -- never hidden."""
        backend = getattr(self.cache, 'backends', {}).get('nvme')
        lookup = getattr(backend, 'prefix_lookup_device', None)
        if lookup is None:
            return
        m = self.prefix_matcher
        try:
            with self.lock:
                index_size = len(m.prefix_index)
            if index_size != self._published_index_size:
                image = m.publish_records()
            else:
                image = None
            sys_tokens = 128
            chain = m._prompt_chain(
                m._user_prompt_id(str(getattr(request, 'user_id', ''))),
                sys_tokens)
            if image is not None:
                matches = lookup(image, chain)
                self._published_index_size = index_size
            else:
                matches = lookup(m.publish_records(), chain)
            with self.lock:
                self.stats['device_lookups'] += 1
                # host registered-on-miss BEFORE this scan ran, so a
                # device hit on a host miss is expected first-sight
                # behavior; the defect signal is host HIT + device empty.
                if host_entry is not None and not matches:
                    self.stats['device_parity_mismatches'] += 1
        except Exception:
            with self.lock:
                self.stats['device_lookup_errors'] += 1

    def check_prefix_cache(self, request: InferenceRequest, model_config: ModelConfig) -> Tuple[Optional[PrefixCacheEntry], int]:
        """
        Checks if the beginning of a request matches a known, cached prefix.

        Mode (offload-stages s6): 'off' = legacy coin flip (continuity
        cells only); 'host' = the real block-chain index. 'device'
        arrives with the P3 plumbing (PIND 11 scan over the published
        record image).

        Returns:
            A tuple containing the PrefixCacheEntry if a hit occurs (or None),
            and the number of remaining (non-prefixed) tokens in the request.
        """
        if self.mode in ('host', 'device'):
            prefix_entry = self.prefix_matcher.detect_system_prompt_real(
                str(getattr(request, 'user_id', '')))
            if self.mode == 'device':
                self._device_lookup_parity(request, prefix_entry)
        else:
            prefix_entry = self.prefix_matcher.detect_system_prompt(request.context_tokens)

        if prefix_entry:
            with self.lock:
                self.stats['prefix_hits'] += 1
                if prefix_entry.prefix_type == PrefixType.SYSTEM_PROMPT:
                    self.stats['system_prompt_reuse'] += 1
                self.stats['bytes_saved'] += prefix_entry.token_count * model_config.kv_cache_size_per_token

            remaining_tokens = max(0, request.context_tokens - prefix_entry.token_count)
            return prefix_entry, remaining_tokens
        else:
            with self.lock:
                self.stats['prefix_misses'] += 1
            return None, request.context_tokens
