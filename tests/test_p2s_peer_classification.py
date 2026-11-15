import json
from pathlib import Path

P = json.loads((Path(__file__).resolve().parents[1] / "data/p2s_business_profiles.json").read_text())


def overlap(a, b):
    return set(P[a]["products"]) & set(P[b]["products"])


def test_sndk_smci_not_direct_despite_same_sector():
    assert not overlap("SNDK", "SMCI")


def test_mu_sndk_direct_memory_overlap():
    assert overlap("MU", "SNDK")


def test_dow_lyb_direct_chemical_overlap():
    assert overlap("DOW", "LYB")


def test_lyb_iff_alb_not_direct():
    assert not overlap("LYB", "IFF")
    assert not overlap("LYB", "ALB")
    assert not overlap("IFF", "ALB")
