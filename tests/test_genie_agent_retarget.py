import json
from pathlib import Path

import pytest
from databricks.bundles.core import Bundle
from databricks.bundles.genie_spaces import GenieSpace

import genie_agent_retarget as gr

EXPORTS = Path(__file__).parent.parent / "example" / "src"


def text(s, m):
    return gr.retarget_text(s, gr.parse_rules(m))


# --- rules -----------------------------------------------------------------

def test_rule_parts_must_match():
    with pytest.raises(ValueError, match="same number of parts"):
        gr.parse_rules({"a.b": "c"})
    with pytest.raises(ValueError):
        gr.parse_rules({"a.b.c.d": "e.f.g.h"})


def test_most_specific_rule_wins():
    m = {"c": "D", "c.s": "C2.S2", "c.s.t": "X.Y.Z"}
    assert text("c.s.t c.s.u c.o.v", m) == "X.Y.Z C2.S2.u D.o.v"


def test_single_pass_no_chaining():
    # a -> b and b -> a must swap, not chain back
    assert text("a.s.t b.s.t", {"a": "b", "b": "a"}) == "b.s.t a.s.t"
    assert text("a.s.t", {"a.s": "b.s", "b": "c"}) == "b.s.t"


# --- text matching ---------------------------------------------------------

def test_table_rule_matches_without_trailing_dot():
    # a prefix-plus-dot matcher misses table rules in `FROM c.s.t WHERE`
    assert text("FROM c.s.t WHERE x", {"c.s.t": "d.e.f"}) == "FROM d.e.f WHERE x"


def test_column_qualified_by_full_name():
    assert text("c.s.t.status_id = 2", {"c.s": "d.e"}) == "d.e.t.status_id = 2"


def test_no_match_inside_longer_identifiers_or_chains():
    m = {"c.s": "d.e"}
    assert text("xc.s.t c.sx.t a.c.s.t", m) == "xc.s.t c.sx.t a.c.s.t"
    # 1- and 2-part names are never touched, even by a catalog rule
    assert text("c.s c", {"c": "d"}) == "c.s c"


def test_case_insensitive_and_backticks_preserved():
    m = {"Main.Gold": "dev.gold_v2"}
    assert text("FROM MAIN.GOLD.auth", m) == "FROM dev.gold_v2.auth"
    assert text("FROM `main`.`gold`.`auth`", m) == "FROM `dev`.`gold_v2`.`auth`"
    assert text("FROM main.`gold`.auth", m) == "FROM dev.`gold_v2`.auth"
    assert text("`my-cat`.s.t", {"my-cat": "prod-cat"}) == "`prod-cat`.s.t"


def test_backticks_in_rules():
    rules = gr.parse_rules({"`main`.`gold`": "dev.gold", "`my.cat`.s": "`new.cat`.s"})
    assert rules == {("main", "gold"): ("dev", "gold"), ("my.cat", "s"): ("new.cat", "s")}
    assert gr.retarget_text("FROM `my.cat`.s.t", rules) == "FROM `new.cat`.s.t"
    with pytest.raises(ValueError, match="expected catalog"):
        gr.parse_rules({"a..b": "c.d"})


def test_identity_mapping_is_noop():
    assert text("c.s.t", {"c.s": "C.S"}) == "c.s.t"


# --- volume paths ----------------------------------------------------------

def test_volume_path_follows_catalog_schema_and_volume_rules():
    m = {"main": "dev", "main.gold": "dev.gold_v2", "main.intel.docs": "dev.intel.playbooks"}
    assert text("/Volumes/main/gold/raw/", m) == "/Volumes/dev/gold_v2/raw/"
    assert text("see /Volumes/main/intel/docs/a.md here", m) == "see /Volumes/dev/intel/playbooks/a.md here"
    assert text("/Volumes/main/other/v", m) == "/Volumes/dev/other/v"
    assert text("read_files('/Volumes/main/gold/raw/x/*.json')", m) == "read_files('/Volumes/dev/gold_v2/raw/x/*.json')"


def test_volume_path_matching_is_case_insensitive_and_keeps_other_paths():
    m = {"main.gold": "dev.gold_v2"}
    assert text("/Volumes/MAIN/Gold/raw", m) == "/Volumes/dev/gold_v2/raw"
    assert text("/Volumes/other/gold/raw/", m) == "/Volumes/other/gold/raw/"
    assert text("/Volumes/main/gold", m) == "/Volumes/main/gold"            # no volume part
    assert text("/mnt/Volumes/main/gold/raw", m) == "/mnt/Volumes/main/gold/raw"
    assert text("/Volumes/my-cat/s/v", {"my-cat": "prod-cat"}) == "/Volumes/prod-cat/s/v"


def test_volume_path_and_table_name_in_one_text():
    m = {"main.gold": "dev.gold_v2"}
    assert (text("COPY INTO main.gold.t FROM '/Volumes/main/gold/raw/'", m)
            == "COPY INTO dev.gold_v2.t FROM '/Volumes/dev/gold_v2/raw/'")


# --- whole space -----------------------------------------------------------

def _space():
    return {
        "data_sources": {"tables": [{"identifier": "c.gold.zeta"}, {"identifier": "c.intel.oss_iocs"}]},
        "instructions": {
            "text_instructions": [{"content": ["Use c.gold.zeta for logins."]}],
            "sql_snippets": {"filters": [{"sql": ["c.gold.zeta.status_id = 2"]}]},
            "join_specs": [{"left": {"identifier": "c.gold.zeta"},
                            "right": {"identifier": "c.intel.oss_iocs"},
                            "sql": ["zeta.ip = oss_iocs.ioc"]}],
        },
        "benchmarks": {"questions": [{"answer": [{"content": ["SELECT * FROM c.intel.oss_iocs"]}]}]},
    }


def test_space_rewrites_all_sections_renames_join_qualifiers_and_sorts():
    out, renamed = gr.retarget_space(_space(), gr.parse_rules(
        {"c.gold": "p.ocsf", "c.intel.oss_iocs": "p.intel.iocs"}))
    assert renamed == {"c.gold.zeta": "p.ocsf.zeta", "c.intel.oss_iocs": "p.intel.iocs"}
    assert [t["identifier"] for t in out["data_sources"]["tables"]] == ["p.intel.iocs", "p.ocsf.zeta"]
    assert out["instructions"]["text_instructions"][0]["content"] == ["Use p.ocsf.zeta for logins."]
    assert out["instructions"]["sql_snippets"]["filters"][0]["sql"] == ["p.ocsf.zeta.status_id = 2"]
    assert out["instructions"]["join_specs"][0]["sql"] == ["zeta.ip = iocs.ioc"]
    assert out["benchmarks"]["questions"][0]["answer"][0]["content"] == ["SELECT * FROM p.intel.iocs"]


def test_table_rename_follows_short_qualifiers_in_sql_only():
    space = {
        "data_sources": {"tables": [{"identifier": "c.intel.oss_iocs"}]},
        "instructions": {
            "text_instructions": [{"content": ["Filter oss_iocs.ioc_type first."]}],
            "sql_snippets": {"filters": [{"sql": ["oss_iocs.ioc_type = 'URL'"]}],
                             "measures": [{"sql": ["COUNT(DISTINCT oss_iocs.ioc)"], "display_name": "x"}]},
            "example_question_sqls": [{"sql": ["SELECT oss_iocs.ioc FROM c.intel.oss_iocs"]}],
        },
    }
    out, _ = gr.retarget_space(space, gr.parse_rules({"c.intel.oss_iocs": "p.intel.iocs"}))
    ins = out["instructions"]
    assert ins["sql_snippets"]["filters"][0]["sql"] == ["iocs.ioc_type = 'URL'"]
    assert ins["sql_snippets"]["measures"][0]["sql"] == ["COUNT(DISTINCT iocs.ioc)"]
    assert ins["example_question_sqls"][0]["sql"] == ["SELECT iocs.ioc FROM p.intel.iocs"]
    assert ins["text_instructions"][0]["content"] == ["Filter oss_iocs.ioc_type first."]  # free text kept


def test_short_qualifier_not_renamed_when_ambiguous_or_alias():
    # another table keeps the old short name -> `t.` is ambiguous, leave it
    space = {"data_sources": {"tables": [{"identifier": "c.a.t"}, {"identifier": "c.b.t"}]},
             "instructions": {"sql_snippets": {"filters": [{"sql": ["t.x = 1"]}]}}}
    out, _ = gr.retarget_space(space, gr.parse_rules({"c.a.t": "c.a.u"}))
    assert out["instructions"]["sql_snippets"]["filters"][0]["sql"] == ["t.x = 1"]
    # an explicit join-spec alias equal to the old name stays
    space = {"data_sources": {"tables": [{"identifier": "c.a.t"}, {"identifier": "c.a.v"}]},
             "instructions": {"join_specs": [{"left": {"identifier": "c.a.t", "alias": "t"},
                                              "right": {"identifier": "c.a.v"}, "sql": ["t.id = v.id"]}]}}
    out, _ = gr.retarget_space(space, gr.parse_rules({"c.a.t": "c.a.u"}))
    assert out["instructions"]["join_specs"][0]["sql"] == ["t.id = v.id"]


def test_space_volumes_are_rewritten_and_sorted():
    space = {"data_sources": {"volumes": [{"path": "/Volumes/b/s/v/", "description": ["kept"]},
                                          {"path": "/Volumes/z/s/v/"}]}}
    out, renamed = gr.retarget_space(space, gr.parse_rules({"z.s": "a.s"}))
    assert out["data_sources"]["volumes"] == [{"path": "/Volumes/a/s/v/"},
                                              {"path": "/Volumes/b/s/v/", "description": ["kept"]}]
    assert renamed == {}           # only table identifiers are reported


def test_no_keys_added():
    space = {"data_sources": {"tables": [{"identifier": "c.a.t"}]},
             "instructions": {"sql_snippets": {"filters": [{"display_name": "no sql"}]}},
             "benchmarks": {"questions": [{"answer": [{"format": "SQL"}]}]}}
    out, _ = gr.retarget_space(space, gr.parse_rules({"c.a.t": "c.a.u"}))
    assert out["instructions"]["sql_snippets"]["filters"][0] == {"display_name": "no sql"}
    assert out["benchmarks"]["questions"][0]["answer"][0] == {"format": "SQL"}


RULES = {
    "fe_lakewatch_catalog.gold": "fe_lakewatch_catalog.genie_demo_ocsf",
    "fe_lakewatch_catalog.lakewatch_field": "fe_lakewatch_catalog.genie_demo_intel",
    "fe_lakewatch_catalog.lakewatch_field.oss_iocs": "fe_lakewatch_catalog.genie_demo_intel.iocs",
}


@pytest.mark.parametrize("path", sorted(EXPORTS.glob("*.geniespace.json")), ids=lambda p: p.stem)
def test_real_exports_leave_no_source_references(path):
    src = json.loads(path.read_text())
    out, renamed = gr.retarget_space(src, gr.parse_rules(RULES))
    body = json.dumps(out)
    assert renamed
    assert "fe_lakewatch_catalog.gold." not in body
    assert "fe_lakewatch_catalog.lakewatch_field." not in body
    assert "oss_iocs." not in body  # short qualifiers in snippets follow the table rename
    # nothing else changed: the reverse rules restore the original exactly
    back, _ = gr.retarget_space(out, gr.parse_rules({v: k for k, v in RULES.items()}))
    assert back == src


# --- DABs mutator ----------------------------------------------------------

def _bundle(mapping):
    return Bundle(target="dev", variables={gr.MAPPING_VARIABLE: mapping})


def test_mutator_inlines_rewritten_body_and_clears_file_path():
    space = GenieSpace(title="t", file_path="../src/x.geniespace.json", serialized_space=json.dumps(_space()))
    out = gr.retarget_genie_spaces.function(_bundle({"c": "p"}), space)
    assert out.file_path is None
    assert json.loads(out.serialized_space)["data_sources"]["tables"][0]["identifier"] == "p.gold.zeta"


def test_mutator_accepts_dict_body_and_noop_mapping():
    space = GenieSpace(title="t", serialized_space=_space())
    assert gr.retarget_genie_spaces.function(_bundle({}), space) is space
    assert gr.retarget_genie_spaces.function(_bundle({"c": "c"}), space) is space
    assert gr.retarget_genie_spaces.function(_bundle({"c": "p"}), GenieSpace(title="t")).serialized_space is None


def test_mutator_requires_variable():
    space = GenieSpace(title="t", serialized_space=_space())
    with pytest.raises(ValueError, match="genie_table_mapping"):
        gr.retarget_genie_spaces.function(Bundle(target="dev", variables={}), space)


# --- CLI -------------------------------------------------------------------

def test_cli_reverse_mapping(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(_space()))
    assert gr.main([str(f), "-m", "c.gold=prod.gold", "--in-place"]) == 0
    assert "c.gold.zeta -> prod.gold.zeta" in capsys.readouterr().err
    assert gr.main([str(f), "-m", "prod.gold=c.gold", "--in-place"]) == 0
    assert json.loads(f.read_text())["data_sources"]["tables"][0]["identifier"] == "c.gold.zeta"
