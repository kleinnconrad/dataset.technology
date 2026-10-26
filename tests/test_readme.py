"""Checks that the data model diagrams, the example records and the variable overview of the README match the schema.

The entity relationship diagrams list every field of every dataset, the
README shows one example record per dataset, and the variable overview names
fields of the datasets. These tests fail when a field is added, renamed or
removed in ``schema.py`` without updating the README.
"""
import json
import re
from pathlib import Path

import pytest

from schema import DATASET_MODELS, RecordBase

README = Path(__file__).resolve().parents[1] / "README.md"
COMMON_ENTITY = "common_fields"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _diagram_bodies(text: str) -> list[str]:
    """Returns the bodies of all erDiagram blocks of the README."""
    bodies = re.findall(r"```mermaid\s*\nerDiagram\n(.*?)```", text, flags=re.DOTALL)
    assert bodies, "README has no erDiagram"
    return bodies


def diagram_entities(text: str) -> dict[str, list[str]]:
    """Returns the attribute names of every entity of the erDiagrams in the README.

    Args:
        text: The README.

    Returns:
        dict[str, list[str]]: Attribute names in diagram order, by entity name. An entity
        that appears in several diagrams must have the same attributes in each.
    """
    entities: dict[str, list[str]] = {}
    for body in _diagram_bodies(text):
        for name, block in re.findall(r"^\s*(\w+)\s*\{\n(.*?)^\s*\}", body, flags=re.DOTALL | re.MULTILINE):
            attributes = [line.split()[1] for line in block.strip().splitlines() if line.strip()]
            assert entities.get(name, attributes) == attributes, f"entity {name} differs between diagrams"
            entities[name] = attributes
    return entities


def example_records(text: str) -> dict[str, list[dict]]:
    """Returns the example records of the README by dataset.

    Args:
        text: The README.

    Returns:
        dict[str, list[dict]]: Parsed JSON examples by dataset name.
    """
    section = text.split("## Example Records", 1)[1].split("\n## ", 1)[0]
    examples: dict[str, list[dict]] = {}
    for dataset, body in re.findall(r"<summary><code>(\w+)</code>.*?</summary>\s*```json\n(.*?)\n```", section,
                                    flags=re.DOTALL):
        examples.setdefault(dataset, []).append(json.loads(body))
    return examples


def test_common_fields_entity_lists_the_fields_of_every_record() -> None:
    entities = diagram_entities(_readme())
    assert entities[COMMON_ENTITY] == list(RecordBase.model_fields)


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_dataset_entity_lists_all_fields_of_its_model(dataset: str) -> None:
    entities = diagram_entities(_readme())
    expected = [name for name in DATASET_MODELS[dataset].model_fields if name not in RecordBase.model_fields]
    assert sorted(entities.get(dataset, [])) == sorted(expected)


def test_every_diagram_attribute_has_a_comment() -> None:
    attribute_lines = [line.strip() for body in _diagram_bodies(_readme()) for line in body.splitlines()
                       if line.strip() and not line.strip().endswith(("{", "}")) and "--" not in line
                       and ".." not in line]
    assert attribute_lines
    assert all(re.search(r'"[^"]+"$', line) for line in attribute_lines), \
        [line for line in attribute_lines if not re.search(r'"[^"]+"$', line)]


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_example_record_validates_against_its_model(dataset: str) -> None:
    examples = example_records(_readme())
    assert len(examples.get(dataset, [])) == 1, f"README needs exactly one example of {dataset}"
    DATASET_MODELS[dataset].model_validate(examples[dataset][0])


def variable_overview_rows(text: str) -> list[tuple[str, str]]:
    """Returns the variable and the dataset of every row of the variable overview of the README.

    Args:
        text: The README.

    Returns:
        list[tuple[str, str]]: Text of the variable cell and dataset name, in table order.
    """
    section = text.split("## Variable Overview", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| (.+?) \| `(\w+)` \|", section, flags=re.MULTILINE)


def test_variable_overview_names_content_fields_of_the_datasets() -> None:
    rows = variable_overview_rows(_readme())
    assert rows
    assert len(set(rows)) == len(rows), "variable overview lists a field twice"
    for variable, dataset in rows:
        name = variable.strip("`")
        assert variable == f"`{name}`", variable
        assert dataset in DATASET_MODELS, dataset
        assert name in DATASET_MODELS[dataset].model_fields, (variable, dataset)
        assert name not in RecordBase.model_fields, (variable, dataset)
