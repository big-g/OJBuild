"""Trusted runtime adapters own validation, fields and executable contracts."""

from __future__ import annotations

import ast
import math
import operator
import re
from copy import deepcopy
from dataclasses import dataclass

TRANSFORMS = ("upper", "lower", "reverse", "length", "identity")
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_ALLOWED = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.UAdd,
    ast.USub,
)


def finite_number(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Formula values must be numbers")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError("Formula number exceeds its limit") from None
    if not math.isfinite(number) or abs(number) > limit:
        raise ValueError("Formula number exceeds its limit")
    return number


def parse_formula(config):
    if not isinstance(config, dict) or set(config) != {"expression", "variables"}:
        raise ValueError("Formula requires an expression and variables")
    expr, variables = config["expression"], config["variables"]
    if not isinstance(expr, str) or not 1 <= len(expr) <= 512:
        raise ValueError("Formula expression must contain 1–512 characters")
    if (
        not isinstance(variables, list)
        or not 1 <= len(variables) <= 8
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", v)
            for v in variables
        )
        or len(set(variables)) != len(variables)
    ):
        raise ValueError("Declare 1–8 unique lowercase variable names")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, RecursionError):
        raise ValueError("Invalid formula expression") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > 128:
        raise ValueError("Formula exceeds 128 syntax elements")
    if any(not isinstance(node, _ALLOWED) for node in nodes):
        raise ValueError("Only numbers, variables, parentheses and + - * / are allowed")
    if {node.id for node in nodes if isinstance(node, ast.Name)} != set(variables):
        raise ValueError("Formula variables must exactly match the declared names")
    for node in nodes:
        if isinstance(node, ast.Constant):
            finite_number(node.value, 1e12)
    pending = [(tree.body, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 32:
            raise ValueError("Formula exceeds 32 levels of nesting")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


def evaluate_formula(config, params):
    tree = parse_formula(config)
    if set(params) != set(config["variables"]):
        raise ValueError("Supply exactly the declared formula variables")
    values = {key: finite_number(value, 1e12) for key, value in params.items()}

    def visit(node, depth=0):
        if depth > 32:
            raise ValueError("Formula exceeds 32 levels of nesting")
        if isinstance(node, ast.Constant):
            return finite_number(node.value, 1e12)
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            try:
                value = _OPS[type(node.op)](
                    visit(node.left, depth + 1), visit(node.right, depth + 1)
                )
            except ZeroDivisionError:
                raise ValueError("Formula division by zero") from None
            return finite_number(value, 1e24)
        raise ValueError("Invalid formula element")

    return visit(tree.body)


@dataclass(frozen=True)
class RuntimeAdapter:
    adapter_id: str
    label: str
    description: str
    validator_version: str
    fields: tuple[dict, ...]
    default_config: dict

    def metadata(self):
        return deepcopy(
            {
                "adapter_id": self.adapter_id,
                "label": self.label,
                "description": self.description,
                "validator_version": self.validator_version,
                "fields": self.fields,
                "default_config": self.default_config,
            }
        )

    def validate(self, config):
        if self.adapter_id == "text_transform":
            if (
                not isinstance(config, dict)
                or set(config) != {"transform"}
                or config["transform"] not in TRANSFORMS
            ):
                raise ValueError("Unsupported transformation")
        elif self.adapter_id == "numeric_formula":
            parse_formula(config)
        else:
            raise ValueError("Unsupported runtime adapter")
        return deepcopy(config)

    def tool_data(self, definition, config):
        if self.adapter_id == "text_transform":
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string", "maxLength": 32768}},
                "required": ["input"],
                "additionalProperties": False,
            }
            action = {"type": "transform", "transform": config["transform"]}
        else:
            parameters = {
                "type": "object",
                "properties": {
                    v: {"type": "number", "minimum": -1e12, "maximum": 1e12}
                    for v in config["variables"]
                },
                "required": list(config["variables"]),
                "additionalProperties": False,
            }
            action = {"type": "bounded_formula", **deepcopy(config)}
        return {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": parameters,
            "action": action,
        }


_ADAPTERS = {
    "text_transform": RuntimeAdapter(
        "text_transform",
        "Text transformation",
        "Transform one text input, up to 32,768 characters.",
        "transform-v1",
        (
            {
                "name": "transform",
                "label": "Transformation",
                "type": "select",
                "required": True,
                "options": [
                    {"value": key, "label": label}
                    for key, label in zip(
                        TRANSFORMS,
                        (
                            "Uppercase",
                            "Lowercase",
                            "Reverse text",
                            "Character count",
                            "Keep text unchanged",
                        ),
                    )
                ],
            },
        ),
        {"transform": "upper"},
    ),
    "numeric_formula": RuntimeAdapter(
        "numeric_formula",
        "Numeric formula",
        "Calculate with up to eight numeric inputs using + - * / and parentheses.",
        "formula-v1",
        (
            {
                "name": "expression",
                "label": "Formula",
                "type": "text",
                "required": True,
                "max_length": 512,
                "placeholder": "value * 1.8 + 32",
            },
            {
                "name": "variables",
                "label": "Variables",
                "type": "string_list",
                "required": True,
                "max_length": 263,
                "placeholder": "value",
                "description": "1–8 unique lowercase names, separated by commas.",
            },
        ),
        {"expression": "value * 1.8 + 32", "variables": ["value"]},
    ),
}


def get_adapter(definition):
    identity = definition.get("adapter_id", "text_transform")
    if not isinstance(identity, str) or identity not in _ADAPTERS:
        raise ValueError("Unsupported runtime adapter")
    return _ADAPTERS[identity]


def adapter_config(definition):
    return (
        definition["config"]
        if "adapter_id" in definition
        else {"transform": definition["transform"]}
    )


def list_adapters():
    return [adapter.metadata() for adapter in _ADAPTERS.values()]
