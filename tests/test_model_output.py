"""Property-based fuzzing of untrusted model output at the curation boundary.

Whatever a model returns (malformed JSON, wrong types, unknown IDs, crops outside
the image, invented captions), the curator either accepts a locally validated
decision or falls back; it never raises and never selects a work outside the pool.
"""

from typing import Any
from unittest.mock import patch

from hypothesis import HealthCheck, event, given, settings, strategies as st

from artwall.curation import ModelCurator, Taste, choose_composition
from artwall.models import ANCHORS, LAYOUT_COUNTS
from tests.support import HomeTest

_POOL_IDS = ("artic:fuzz-0", "artic:fuzz-1", "artic:fuzz-2")
_json = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20),
                     lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
                     max_leaves=12)
_number = st.floats(min_value=-0.5, max_value=1.5, allow_nan=False) | st.integers(-2, 2) | _json
_crop = st.fixed_dictionaries({}, optional={key: _number for key in ("x", "y", "width", "height")}) | _json
_artwork_id = st.sampled_from(_POOL_IDS) | st.text(max_size=12)
_entry = st.fixed_dictionaries({"id": _artwork_id}, optional={"crop": _crop}) | _json
# Mostly decision-shaped objects, so fuzzing reaches validation beyond the parser.
_decision = st.fixed_dictionaries({}, optional={
    "layout": st.sampled_from(sorted(LAYOUT_COUNTS)) | st.text(max_size=12) | _json,
    "primary_artwork_id": _artwork_id | _json,
    "artworks": st.lists(_entry, max_size=4) | _json,
    "caption_anchor": st.sampled_from(ANCHORS) | _json,
    "caption_max_width_px": st.integers(-100, 5000) | _json,
    "use_gradient": st.booleans() | _json,
    "description": st.text(max_size=300) | _json,
    "relationship": st.text(max_size=80) | _json,
    "curator_reason": st.text(max_size=80) | _json,
})

_MUSEUM_TEXT = ("A quiet river bends below tall trees in soft evening light, while two small boats rest "
                "near the far bank and pale clouds drift across a wide open sky.")
# Well-formed answers, sometimes with one field replaced, so the accepted path is exercised too.
_valid = st.builds(lambda work, anchor, extra: {"layout": "single", "primary_artwork_id": work,
                                                 "artworks": [{"id": work}], "caption_anchor": anchor,
                                                 "description": _MUSEUM_TEXT, "curator_reason": "Calm river.",
                                                 **extra},
                   st.sampled_from(_POOL_IDS), st.sampled_from(ANCHORS),
                   st.dictionaries(st.sampled_from(["layout", "artworks", "caption_max_width_px", "description"]),
                                   _json, max_size=1))


class ModelOutputTests(HomeTest):
    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(answers=st.lists(_valid | _decision | _json, min_size=1, max_size=3), max_artworks=st.integers(1, 3),
           ratio=st.floats(0.5, 4.0))
    def test_any_model_answer_yields_a_valid_decision_from_the_pool(self, answers: list[Any],
                                                                    max_artworks: int, ratio: float):
        if not hasattr(self, "pool"):
            self.pool = [self.artwork(f"fuzz-{index}", size, title=f"Study {index}", artist="An Etcher",
                                      museum_description=_MUSEUM_TEXT)
                         for index, size in enumerate(((1600, 1200), (900, 1400), (2400, 900)))]
        replies = iter(answers)
        with patch.object(ModelCurator, "_chat", side_effect=lambda *args, **kwargs: next(replies, answers[-1])), \
                patch("artwall.curation.LOG"):  # Each rejected answer logs a fallback warning.
            decision = choose_composition(self.pool, Taste(), max_artworks=max_artworks, screen_ratio=ratio,
                                          curator=ModelCurator())
        event("accepted" if "unavailable or invalid" not in decision.curator_reason else "fell back")
        self.assertTrue(set(decision.artwork_ids) <= set(_POOL_IDS))
        self.assertIn(decision.primary_artwork_id, decision.artwork_ids)
        self.assertEqual(len(decision.artwork_ids), LAYOUT_COUNTS[decision.layout])
        self.assertLessEqual(len(decision.artwork_ids), max_artworks)
        for work in decision.artworks:
            if work.crop:
                self.assertTrue(0 <= work.crop.x and work.crop.x + work.crop.width <= 1.000001)
                self.assertTrue(0 <= work.crop.y and work.crop.y + work.crop.height <= 1.000001)
