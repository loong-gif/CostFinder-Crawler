from utils.apify_client import extract_default_dataset_id


def test_extract_default_dataset_id_reads_top_level_field():
    assert extract_default_dataset_id({"defaultDatasetId": "dataset-top"}) == "dataset-top"


def test_extract_default_dataset_id_reads_nested_data_field():
    assert extract_default_dataset_id({"data": {"defaultDatasetId": "dataset-data"}}) == "dataset-data"


def test_extract_default_dataset_id_reads_nested_dataset_object():
    payload = {"data": {"defaultDataset": {"id": "dataset-object"}}}
    assert extract_default_dataset_id(payload) == "dataset-object"


def test_extract_default_dataset_id_returns_empty_string_when_missing():
    assert extract_default_dataset_id({}) == ""
