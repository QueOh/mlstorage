#!/usr/bin/env python3
"""Unit tests for SPDK inventory default loader."""

from __future__ import annotations

import pytest

from kv_cache._compat import HAS_YAML
from kv_cache.cpcs_spdk_inventory import load_spdk_inventory_defaults


pytestmark = pytest.mark.skipif(not HAS_YAML, reason="pyyaml not available")


def test_inventory_loader_accepts_hex_and_decimal_runtime_fields(tmp_path):
    inv = tmp_path / "inventory.yaml"
    inv.write_text(
        """
nvmeof:
  trtype: TCP
  traddr: 10.0.0.8
  trsvcid: "4420"
  nqn: nqn.2026-06.io.spdk:test
  hostnqn: nqn.2026-06.io.spdk:test-host
  dataset_nsid: "0x1"
  slm_nsid: "0x65"
  cpcs_nsid: "0xC8"
runtime:
  direct_probe_nsid: "0x2"
  direct_probe_offset: "0x1000"
  direct_probe_length: "0x2000"
  direct_probe_lba_bytes: "0x200"
  slm_rw_lba_bytes: "0x200"
  slm_read_address_mode: byte
  slm_write_address_mode: lba
  cpcs_program_pind: "0x5"
  cpcs_rsid: "0x9"
  mrs_default_length_bytes: "0x10000"
  mrs_align_bytes: "0x1000"
  load_program_pind: "0x6"
  load_program_chunk_bytes: "0x4000"
  load_program_ptype: "0xC0"
  load_program_pit: "0x1"
  load_program_puid: "0xEBF00001"
  cpcs_rsid_pack_store: "0x21"
  cpcs_program_pind_unpack_load: "0x22"
        """.strip(),
        encoding="utf-8",
    )

    out = load_spdk_inventory_defaults(str(inv))
    assert out["trtype"] == "TCP"
    assert out["traddr"] == "10.0.0.8"
    assert out["subnqn"] == "nqn.2026-06.io.spdk:test"
    assert out["hostnqn"] == "nqn.2026-06.io.spdk:test-host"

    assert out["dataset_nsid"] == 2
    assert out["slm_nsid"] == 0x65
    assert out["cpcs_nsid"] == 0xC8
    assert out["direct_probe_offset"] == 0x1000
    assert out["direct_probe_length"] == 0x2000
    assert out["direct_probe_lba_bytes"] == 0x200
    assert out["cpcs_slm_rw_lba_bytes"] == 0x200
    assert out["cpcs_slm_read_address_mode"] == "byte"
    assert out["cpcs_slm_write_address_mode"] == "lba"

    assert out["cpcs_program_pind"] == 0x5
    assert out["cpcs_rsid"] == 0x9
    assert out["cpcs_mrs_default_length_bytes"] == 0x10000
    assert out["cpcs_mrs_align_bytes"] == 0x1000
    assert out["cpcs_load_program_pind"] == 0x6
    assert out["cpcs_load_program_chunk_bytes"] == 0x4000
    assert out["cpcs_load_program_ptype"] == 0xC0
    assert out["cpcs_load_program_pit"] == 0x1
    assert out["cpcs_load_program_puid"] == 0xEBF00001
    assert out["cpcs_rsid_pack_store"] == 0x21
    assert out["cpcs_program_pind_unpack_load"] == 0x22


def test_inventory_loader_boolean_strings(tmp_path):
    inv = tmp_path / "inventory_bool.yaml"
    inv.write_text(
        """
nvmeof:
  trtype: TCP
runtime:
  bootstrap_install_builtins: "yes"
  bootstrap_list_programs: "true"
  bootstrap_list_mrs: "on"
  auto_create_mrs: "1"
  load_program_set_default_pind: "true"
  activate_loaded_program: "1"
        """.strip(),
        encoding="utf-8",
    )

    out = load_spdk_inventory_defaults(str(inv))
    assert out["cpcs_bootstrap_install_builtins"] is True
    assert out["cpcs_bootstrap_list_programs"] is True
    assert out["cpcs_bootstrap_list_mrs"] is True
    assert out["cpcs_auto_create_mrs"] is True
    assert out["cpcs_load_program_set_default_pind"] is True
    assert out["cpcs_activate_loaded_program"] is True

