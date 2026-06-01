"""
Helpers for loading SPDK CPCS inventory defaults.

The schema follows spdk/test/cpcs inventory files used by existing
real-SPDK automation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from kv_cache._compat import HAS_YAML

if HAS_YAML:
    import yaml


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    return int(text, 0)


def load_spdk_inventory_defaults(path: str) -> Dict[str, Any]:
    """
    Load passthru-related defaults from an SPDK inventory YAML.

    Returned keys are normalized to this benchmark's CLI naming:
      trtype, traddr, trsvcid, subnqn, hostnqn, src_addr, src_svcid,
      passthru_lcores, dataset_nsid, slm_nsid, cpcs_nsid,
      direct_probe_offset, direct_probe_length, direct_probe_lba_bytes,
      cpcs_slm_rw_lba_bytes, cpcs_slm_read_address_mode, cpcs_slm_write_address_mode,
      cpcs_program_pind, cpcs_rsid, optional bootstrap/program/MRS knobs,
      and optional per-op overrides
    """
    if not HAS_YAML:
        raise RuntimeError("pyyaml is required to parse --spdk-inventory")

    inv_path = Path(path).expanduser().resolve()
    if not inv_path.exists():
        raise FileNotFoundError(f"SPDK inventory file not found: {inv_path}")

    with inv_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("SPDK inventory root must be a mapping")

    nvmeof = raw.get("nvmeof", {})
    runtime = raw.get("runtime", {})

    out: Dict[str, Any] = {}
    if isinstance(nvmeof, dict):
        if nvmeof.get("trtype") is not None:
            out["trtype"] = str(nvmeof["trtype"])
        if nvmeof.get("traddr") is not None:
            out["traddr"] = str(nvmeof["traddr"])
        if nvmeof.get("trsvcid") is not None:
            out["trsvcid"] = str(nvmeof["trsvcid"])
        if nvmeof.get("nqn") is not None:
            out["subnqn"] = str(nvmeof["nqn"])
        if nvmeof.get("hostnqn") is not None:
            out["hostnqn"] = str(nvmeof["hostnqn"])
        if nvmeof.get("src_addr") is not None:
            out["src_addr"] = str(nvmeof["src_addr"])
        if nvmeof.get("src_svcid") is not None:
            out["src_svcid"] = str(nvmeof["src_svcid"])

        dataset_bdev = nvmeof.get("dataset_bdev")
        if isinstance(dataset_bdev, str):
            # Convention: Nvme0n1 -> NSID 1, Nvme1n2 -> NSID 2
            lower = dataset_bdev.lower()
            idx = lower.rfind("n")
            if idx >= 0:
                nsid_text = lower[idx + 1 :]
                if nsid_text.isdigit():
                    out["dataset_nsid"] = int(nsid_text)

        if nvmeof.get("dataset_nsid") is not None:
            out["dataset_nsid"] = _to_int(nvmeof["dataset_nsid"])
        if nvmeof.get("slm_nsid") is not None:
            out["slm_nsid"] = _to_int(nvmeof["slm_nsid"])
        if nvmeof.get("cpcs_nsid") is not None:
            out["cpcs_nsid"] = _to_int(nvmeof["cpcs_nsid"])

    if isinstance(runtime, dict):
        if runtime.get("initiator_passthru_lcores") is not None:
            out["passthru_lcores"] = str(runtime["initiator_passthru_lcores"])
        if runtime.get("direct_probe_nsid") is not None:
            out["dataset_nsid"] = _to_int(runtime["direct_probe_nsid"])
        if runtime.get("dataset_nsid") is not None:
            out["dataset_nsid"] = _to_int(runtime["dataset_nsid"])
        if runtime.get("slm_nsid") is not None:
            out["slm_nsid"] = _to_int(runtime["slm_nsid"])
        if runtime.get("cpcs_nsid") is not None:
            out["cpcs_nsid"] = _to_int(runtime["cpcs_nsid"])
        if runtime.get("direct_probe_offset") is not None:
            out["direct_probe_offset"] = _to_int(runtime["direct_probe_offset"])
        if runtime.get("direct_probe_length") is not None:
            out["direct_probe_length"] = _to_int(runtime["direct_probe_length"])
        if runtime.get("direct_probe_lba_bytes") is not None:
            out["direct_probe_lba_bytes"] = _to_int(runtime["direct_probe_lba_bytes"])
        if runtime.get("slm_rw_lba_bytes") is not None:
            out["cpcs_slm_rw_lba_bytes"] = _to_int(runtime["slm_rw_lba_bytes"])
        if runtime.get("slm_read_address_mode") is not None:
            out["cpcs_slm_read_address_mode"] = str(runtime["slm_read_address_mode"])
        if runtime.get("slm_write_address_mode") is not None:
            out["cpcs_slm_write_address_mode"] = str(runtime["slm_write_address_mode"])
        if runtime.get("cpcs_program_pind") is not None:
            out["cpcs_program_pind"] = _to_int(runtime["cpcs_program_pind"])
        if runtime.get("cpcs_rsid") is not None:
            out["cpcs_rsid"] = _to_int(runtime["cpcs_rsid"])
        if runtime.get("bootstrap_subsystem_nqn") is not None:
            out["bootstrap_subsystem_nqn"] = str(runtime["bootstrap_subsystem_nqn"])
        if runtime.get("bootstrap_install_builtins") is not None:
            out["cpcs_bootstrap_install_builtins"] = _to_bool(runtime["bootstrap_install_builtins"])
        if runtime.get("bootstrap_list_programs") is not None:
            out["cpcs_bootstrap_list_programs"] = _to_bool(runtime["bootstrap_list_programs"])
        if runtime.get("bootstrap_list_mrs") is not None:
            out["cpcs_bootstrap_list_mrs"] = _to_bool(runtime["bootstrap_list_mrs"])
        if runtime.get("auto_create_mrs") is not None:
            out["cpcs_auto_create_mrs"] = _to_bool(runtime["auto_create_mrs"])
        if runtime.get("mrs_ranges") is not None:
            out["cpcs_mrs_ranges"] = str(runtime["mrs_ranges"])
        if runtime.get("mrs_default_length_bytes") is not None:
            out["cpcs_mrs_default_length_bytes"] = _to_int(runtime["mrs_default_length_bytes"])
        if runtime.get("mrs_align_bytes") is not None:
            out["cpcs_mrs_align_bytes"] = _to_int(runtime["mrs_align_bytes"])
        if runtime.get("mrs_align_mode") is not None:
            out["cpcs_mrs_align_mode"] = str(runtime["mrs_align_mode"])
        if runtime.get("load_program_path") is not None:
            out["cpcs_load_program_path"] = str(runtime["load_program_path"])
        if runtime.get("load_program_pind") is not None:
            out["cpcs_load_program_pind"] = _to_int(runtime["load_program_pind"])
        if runtime.get("load_program_set_default_pind") is not None:
            out["cpcs_load_program_set_default_pind"] = _to_bool(runtime["load_program_set_default_pind"])
        if runtime.get("load_program_chunk_bytes") is not None:
            out["cpcs_load_program_chunk_bytes"] = _to_int(runtime["load_program_chunk_bytes"])
        if runtime.get("load_program_ptype") is not None:
            out["cpcs_load_program_ptype"] = _to_int(runtime["load_program_ptype"])
        if runtime.get("load_program_pit") is not None:
            out["cpcs_load_program_pit"] = _to_int(runtime["load_program_pit"])
        if runtime.get("load_program_puid") is not None:
            out["cpcs_load_program_puid"] = _to_int(runtime["load_program_puid"])
        if runtime.get("activate_loaded_program") is not None:
            out["cpcs_activate_loaded_program"] = _to_bool(runtime["activate_loaded_program"])

        for key in (
            "cpcs_program_pind_pack_store",
            "cpcs_program_pind_unpack_load",
            "cpcs_program_pind_layout_repack",
            "cpcs_program_pind_block_select",
            "cpcs_program_pind_prefix_lookup",
            "cpcs_program_pind_batch_read",
            "cpcs_rsid_pack_store",
            "cpcs_rsid_unpack_load",
            "cpcs_rsid_layout_repack",
            "cpcs_rsid_block_select",
            "cpcs_rsid_prefix_lookup",
            "cpcs_rsid_batch_read",
        ):
            if runtime.get(key) is not None:
                out[key] = _to_int(runtime[key])

    return out
