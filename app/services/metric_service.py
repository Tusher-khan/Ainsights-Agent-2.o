"""Semantic layer management: define, list, edit and look up business metrics."""

from __future__ import annotations

import re

from app.agent import metrics as engine
from app.core.datasource import from_dataset_record
from app.database import metric_repo, repository
from app.utils.exceptions import DatasetNotFoundError
from app.utils.logger import get_logger

log = get_logger("services.metrics")
MAX_METRICS_PER_USER = 100


def _source(dataset_id: str):
    record = repository.get_dataset(dataset_id)  # owner-scoped: someone else's id is "not found"
    if not record:
        raise DatasetNotFoundError()
    return from_dataset_record(record)


def _validated(dataset_id: str, expression: str) -> str:
    source = _source(dataset_id)
    tree = engine.resolve(engine.parse(expression), source)
    return engine.render(tree)  # canonical form with real column names


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not 2 <= len(name) <= 60 or not re.match(r"^[\w][\w %&/().-]*$", name):
        raise engine.MetricError("Give the metric a name of 2–60 letters, numbers or spaces.")
    return name


def list_metrics(dataset_id: str | None = None) -> list[dict]:
    if dataset_id:
        _source(dataset_id)
    return metric_repo.list_for(dataset_id)


def create_metric(dataset_id: str, name: str, expression: str, description: str = "") -> dict:
    name = _clean_name(name)
    if len(metric_repo.list_for()) >= MAX_METRICS_PER_USER:
        raise engine.MetricError(f"You can define up to {MAX_METRICS_PER_USER} metrics.")
    if any(m["name"].lower() == name.lower() for m in metric_repo.list_for(dataset_id)):
        raise engine.MetricError("A metric with that name already exists for this dataset.")
    canonical = _validated(dataset_id, expression)
    return metric_repo.create(dataset_id, name, canonical, (description or "")[:300])


def update_metric(metric_id: str, name: str | None, expression: str | None, description: str | None) -> dict:
    current = metric_repo.get(metric_id)
    if not current:
        raise engine.MetricError("That metric does not exist.")
    fields = {}
    if name is not None:
        fields["name"] = _clean_name(name)
    if expression is not None:
        fields["expression"] = _validated(current["dataset_id"], expression)
    if description is not None:
        fields["description"] = description[:300]
    return metric_repo.update(metric_id, **fields)


def delete_metric(metric_id: str) -> bool:
    return metric_repo.delete(metric_id)


def find_for_question(dataset_id: str, question: str) -> dict | None:
    """The longest-named metric of this dataset that the question mentions."""
    matches = [
        m for m in metric_repo.list_for(dataset_id)
        if re.search(r"(?<![\w])" + re.escape(m["name"].lower()) + r"(?![\w])", question.lower())
    ]
    return max(matches, key=lambda m: len(m["name"])) if matches else None
