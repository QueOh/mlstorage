"""
Configuration loader and global config accessors for KV Cache Benchmark.

Provides YAML-based config loading with strict schema validation,
plus module-level cfg()/get_config()/set_config() accessors.
"""

import logging
from pathlib import Path
from typing import Optional

from kv_cache._compat import HAS_YAML

if HAS_YAML:
    import yaml

logger = logging.getLogger(__name__)


class ConfigLoader:
    """
    Loads and validates benchmark configuration from YAML files.

    Raises errors on invalid/unknown keys to prevent silent misconfigurations
    in MLPerf competition submissions.
    """

    # Define the valid configuration schema with expected types
    VALID_SCHEMA = {
        'model_configs': ...,  # Dynamic keys (model names) with nested model properties
        'user_templates': {
            'chatbot': {'context_range': list, 'generation_range': list, 'think_time_range': list},
            'coding': {'context_range': list, 'generation_range': list, 'think_time_range': list},
            'document': {'context_range': list, 'generation_range': list, 'think_time_range': list},
        },
        'generation_timing': {
            'none': (int, float),
            'fast': (int, float),
            'realistic': (int, float),
        },
        'qos_profiles': {
            'interactive': {'target_latency_p95_ms': (int, float), 'target_latency_p99_ms': (int, float),
                           'target_latency_p999_ms': (int, float), 'target_latency_p9999_ms': (int, float), 'priority': int},
            'responsive': {'target_latency_p95_ms': (int, float), 'target_latency_p99_ms': (int, float),
                          'target_latency_p999_ms': (int, float), 'target_latency_p9999_ms': (int, float), 'priority': int},
            'batch': {'target_latency_p95_ms': (int, float), 'target_latency_p99_ms': (int, float),
                     'target_latency_p999_ms': (int, float), 'target_latency_p9999_ms': (int, float), 'priority': int},
        },
        'qos_distribution': {
            'interactive_probability': (int, float),
            'responsive_threshold': (int, float),
        },
        'eviction': {
            'max_recursion_depth': int,
            'target_usage_ratio': (int, float),
            'large_entry_limit_ratio': (int, float),
            'max_evictions_hard_cap': int,
            'max_evictions_min': int,
        },
        'gpu_backend': {
            'memory_fraction': (int, float),
            'max_eviction_attempts': int,
            'free_memory_threshold': (int, float),
        },
        'prefix_cache': {
            'min_prefix_length': int,
            'max_prefix_entries': int,
            'system_prompt_hit_probability': (int, float),
        },
        'rag': {
            'chunk_size_tokens': int,
            'top_k_chunks': int,
            'max_chunk_bytes': int,
            'request_probability': (int, float),
            'retrieval_distribution': str,
            'max_documents': int,
            'large_model_doc_tokens_min': int,
            'large_model_doc_tokens_max': int,
            'small_model_doc_tokens_min': int,
            'small_model_doc_tokens_max': int,
        },
        'conversation': {
            'max_conversations': int,
            'max_turns_per_conv': int,
            'end_conversation_probability': (int, float),
        },
        'autoscaler': {
            'min_users': int,
            'max_users': int,
            'scale_up_factor': (int, float),
            'scale_down_factor': (int, float),
            'consecutive_samples_required': int,
        },
        'decode': {
            'batch_size': int,
        },
        'sharegpt': {
            'max_context_tokens': int,
            'max_generation_tokens': int,
            'chars_per_token_estimate': int,
        },
        'saturation_detection': {
            'read_latency_p95_threshold_ms': (int, float),
            'write_latency_p95_threshold_ms': (int, float),
            'queue_depth_threshold': int,
            'history_window_size': int,
        },
        'validation_limits': {
            'max_users': int,
            'max_duration_seconds': int,
            'max_gpu_memory_gb': int,
            'max_cpu_memory_gb': int,
        },
        'cpcs': {
            'mode': str,
            'client': str,
            'storage_mode': str,
            'verify_every_n': int,
            'lossy_tolerance': (int, float),
            'block_size_kb': int,
            'batch_size': int,
            'fallback_on_error': bool,
            'arena_path': str,
            'index_path': str,
            'metrics_output': str,
            'spdk_rpc_script': str,
            'spdk_rpc_python': str,
            'spdk_rpc_socket': str,
            'bootstrap_subsystem_nqn': str,
            'bootstrap_check': bool,
            'bootstrap_install_builtins': bool,
            'bootstrap_list_programs': bool,
            'bootstrap_list_mrs': bool,
            'required_rpc_methods': str,
            'spdk_nvme_passthru': str,
            'trtype': str,
            'traddr': str,
            'trsvcid': str,
            'subnqn': str,
            'hostnqn': str,
            'src_addr': str,
            'src_svcid': str,
            'passthru_lcores': str,
            'dataset_nsid': int,
            'slm_nsid': int,
            'cpcs_nsid': int,
            'cpcs_program_pind': int,
            'cpcs_program_pind_pack_store': int,
            'cpcs_program_pind_unpack_load': int,
            'cpcs_program_pind_layout_repack': int,
            'cpcs_program_pind_block_select': int,
            'cpcs_program_pind_prefix_lookup': int,
            'cpcs_program_pind_batch_read': int,
            'cpcs_rsid': int,
            'cpcs_rsid_pack_store': int,
            'cpcs_rsid_unpack_load': int,
            'cpcs_rsid_layout_repack': int,
            'cpcs_rsid_block_select': int,
            'cpcs_rsid_prefix_lookup': int,
            'cpcs_rsid_batch_read': int,
            'auto_create_mrs': bool,
            'mrs_ranges': str,
            'mrs_default_length_bytes': int,
            'mrs_align_bytes': int,
            'mrs_align_mode': str,
            'load_program_path': str,
            'load_program_pind': int,
            'load_program_set_default_pind': bool,
            'load_program_chunk_bytes': int,
            'load_program_ptype': int,
            'load_program_pit': int,
            'load_program_puid': int,
            'activate_loaded_program': bool,
            'direct_probe_offset': int,
            'direct_probe_length': int,
            'direct_probe_lba_bytes': int,
            'slm_rw_lba_bytes': int,
            'slm_read_address_mode': str,
            'slm_write_address_mode': str,
        },
    }

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the ConfigLoader.

        Args:
            config_path: Path to YAML config file. If None, uses built-in defaults.
        """
        self.config_path = config_path
        self.config = {}

        if config_path:
            self._load_and_validate(config_path)

    def _load_and_validate(self, config_path: str) -> None:
        """Load YAML config and validate strictly against schema."""
        if not HAS_YAML:
            raise RuntimeError("pyyaml is required for config file support. Install with: pip install pyyaml")

        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(path, 'r') as f:
            self.config = yaml.safe_load(f) or {}

        # Validate all keys against schema
        self._validate_keys(self.config, self.VALID_SCHEMA, path_prefix='')

        logger.info(f"Loaded configuration from {config_path}")

    def _validate_keys(self, config: dict, schema: dict, path_prefix: str) -> None:
        """Recursively validate config keys against schema. Raises on unknown keys."""
        for key, value in config.items():
            full_path = f"{path_prefix}.{key}" if path_prefix else key

            if key not in schema:
                raise ValueError(f"Unknown configuration key: '{full_path}'. "
                               f"Valid keys at this level: {list(schema.keys())}")

            expected_type = schema[key]

            # Ellipsis (...) means "allow any structure" - skip validation
            if expected_type is ...:
                continue

            # If schema expects a dict, recurse
            if isinstance(expected_type, dict):
                if not isinstance(value, dict):
                    raise ValueError(f"Config key '{full_path}' must be a dict, got {type(value).__name__}")
                self._validate_keys(value, expected_type, full_path)
            else:
                # Validate type
                if isinstance(expected_type, tuple):
                    if not isinstance(value, expected_type):
                        raise ValueError(f"Config key '{full_path}' must be one of {expected_type}, "
                                       f"got {type(value).__name__}")
                elif not isinstance(value, expected_type):
                    raise ValueError(f"Config key '{full_path}' must be {expected_type.__name__}, "
                                   f"got {type(value).__name__}")

    def get(self, *keys, default=None):
        """
        Get a nested configuration value.

        Args:
            *keys: Path to the config value (e.g., 'qos_profiles', 'interactive', 'priority')
            default: Default value if key not found

        Returns:
            The config value or default
        """
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value


# Global config instance (set from main() when --config is provided)
_global_config: Optional[ConfigLoader] = None


def get_config() -> Optional[ConfigLoader]:
    """Get the global configuration loader instance."""
    return _global_config


def set_config(config: ConfigLoader) -> None:
    """Set the global configuration loader instance."""
    global _global_config
    _global_config = config


def cfg(*keys, default=None):
    """
    Get a configuration value from the global config, with fallback to default.

    Args:
        *keys: Path to the config value (e.g., 'qos_profiles', 'interactive', 'priority')
        default: Default value if config not loaded or key not found

    Returns:
        The config value or default
    """
    config = get_config()
    if config is None:
        return default
    return config.get(*keys, default=default)
