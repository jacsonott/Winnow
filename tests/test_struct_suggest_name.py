"""structparse.suggest_name: JSON paths name their column by the full
dotted path; XML keeps the leaf / predicate value."""
from winnow.structparse import suggest_name


def test_json_uses_the_full_path():
    assert suggest_name("$.target.ip", "json") == "target.ip"
    assert suggest_name("$.source.ip", "json") == "source.ip"
    assert suggest_name("$.ip", "json") == "ip"
    assert suggest_name("$.items[0].id", "json") == "items[0].id"
    assert suggest_name('$["odd key"].x', "json") == "odd key.x"
    assert suggest_name("$[0]", "json") == "[0]"
    assert suggest_name("not a path[", "json") == "not a path["


def test_xml_keeps_the_leaf_and_the_predicate():
    assert suggest_name("Event/System/EventID", "xml") == "EventID"
    assert suggest_name("Event/EventData/Data[@Name='TargetUserName']", "xml") == "TargetUserName"
    assert suggest_name("Event/System/TimeCreated@SystemTime", "xml") == "TimeCreated SystemTime"


def test_json_keys_that_start_with_at_are_keys_not_xml_attributes():
    # EvtxECmd payloads follow the XML-to-JSON convention: attributes
    # become "@Name" keys, text becomes "#text". Both are plain keys here;
    # the "@" rules are XML's, and they used to catch these first.
    assert suggest_name("$.EventData.Data[0].@Name", "json") == "EventData.Data[0].@Name"
    assert suggest_name("$.EventData.Data[0].#text", "json") == "EventData.Data[0].#text"
    assert suggest_name("$.System.Provider.@Name", "json") == "System.Provider.@Name"
