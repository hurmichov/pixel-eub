"""Model names and observed ROM chip identifiers; unknown chips fail closed."""

import re
from dataclasses import dataclass

from . import EubError


@dataclass(frozen=True)
class Model:
    codename: str
    name: str
    soc: str
    pack: str | None = None
    experimental: bool = False


MODELS = (
    Model("oriole", "Pixel 6", "gs101"),
    Model("raven", "Pixel 6 Pro", "gs101"),
    Model("bluejay", "Pixel 6a", "gs101"),
    Model("panther", "Pixel 7", "gs201", "gs201"),
    Model("cheetah", "Pixel 7 Pro", "gs201", "gs201"),
    Model("lynx", "Pixel 7a", "gs201", "gs201", True),
    Model("felix", "Pixel Fold", "gs201", "gs201", True),
    Model("tangorpro", "Pixel Tablet", "gs201", "gs201", True),
    Model("shiba", "Pixel 8", "zuma", "zuma/shiba"),
    Model("husky", "Pixel 8 Pro", "zuma", "zuma/husky"),
    Model("akita", "Pixel 8a", "zuma", "zuma/husky", True),
    Model("tokay", "Pixel 9", "zumapro"),
    Model("caiman", "Pixel 9 Pro", "zumapro"),
    Model("komodo", "Pixel 9 Pro XL", "zumapro"),
    Model("comet", "Pixel 9 Pro Fold", "zumapro"),
    Model("tegu", "Pixel 9a", "zumapro"),
    Model("frankel", "Pixel 10", "laguna"),
    Model("blazer", "Pixel 10 Pro", "laguna"),
    Model("mustang", "Pixel 10 Pro XL", "laguna"),
    Model("rango", "Pixel 10 Pro Fold", "laguna"),
)
CHIPS = {"9845": "gs101", "9855": "gs201", "9865": "zuma"}


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def model_for(value: str) -> Model:
    value = normalize(value).removeprefix("google")
    for model in MODELS:
        if value in (normalize(model.name), model.codename):
            return model
    raise EubError("Unknown model. Use 'pixel-eub models' for supported names.")


def soc_for(serial: str) -> str:
    soc = CHIPS.get(serial[1:5]) if serial.startswith("0") else None
    if not soc:
        raise EubError(
            f"Unknown or unverified ROM chip ID in {serial!r}; no upload allowed."
        )
    return soc


def choose_model(soc: str, interactive: bool, ask=input) -> Model:
    candidates = [m for m in MODELS if m.soc == soc]
    if not interactive:
        raise EubError(
            f"Detected {soc}, but ROM does not identify the exact model. "
            "Specify --device: " + ", ".join(m.codename for m in candidates)
        )
    print(f"ROM identifies {soc}; select the actual phone model:")
    for i, model in enumerate(candidates, 1):
        print(f"  {i}. {model.name} ({model.codename})")
    try:
        answer = int(ask("Model number: "))
    except (ValueError, EOFError) as exc:
        raise EubError("No valid model selected; specify --device.") from exc
    if not 1 <= answer <= len(candidates):
        raise EubError("Model number is out of range.")
    return candidates[answer - 1]
