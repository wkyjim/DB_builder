import json
from pathlib import Path

P = json.loads((Path(__file__).resolve().parents[1] / "data/p2t_business_descriptions.json").read_text())


def test_mu_description_is_memory_not_foundry():
    assert "semiconductor memory and storage" in P["MU"]["passage"]
    assert "foundry" not in P["MU"]["passage"].lower()


def test_sndk_description_is_nand_storage():
    assert "NAND flash" in P["SNDK"]["passage"]
    assert "SSDs" in P["SNDK"]["passage"]


def test_business_descriptions_have_official_sources():
    for t in ("MU", "SNDK"):
        assert P[t]["url"].startswith("https://www.sec.gov/Archives/")
        assert "10-K" in P[t]["source"]
