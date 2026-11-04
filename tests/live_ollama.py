"""Explicit end-to-end check against a running local Ollama vision model."""

import json
import os

from PIL import Image, ImageDraw

from tests.support import HomeTest


class LiveOllamaTests(HomeTest):
    def test_curates_a_pair_with_supported_layout_names(self):
        model = os.environ.get("ARTWALL_TEST_MODEL", "qwen3.6:27b")
        base_url = os.environ.get("ARTWALL_TEST_OLLAMA_URL", "http://127.0.0.1:11434")
        self.configure(f"[llm]\nmodel = {json.dumps(model)}\nbase_url = {json.dumps(base_url)}\n"
                       "[curation]\nmax_artworks = 2\n")
        with self.database() as db:
            for identifier, title in (("live-left", "Mountain Study"), ("live-right", "Valley Study")):
                work = self.artwork(identifier, (1600, 1450), title=title, artist="A Landscape Artist",
                                    subject_tags=["mountains"])
                db.upsert_artwork(work)
        self.cli("curate", "--count", "1")
        with self.database() as db:
            (composition,) = db.list_compositions()
        self.assertEqual(set(composition.artwork_ids), {"artic:live-left", "artic:live-right"})
        self.assertEqual(composition.decision.layout, "diptych")
        self.assertNotIn("Local model unavailable or invalid", composition.decision.curator_reason)

    def test_curates_a_cached_artwork_with_the_real_model(self):
        model = os.environ.get("ARTWALL_TEST_MODEL", "qwen3.6:27b")
        base_url = os.environ.get("ARTWALL_TEST_OLLAMA_URL", "http://127.0.0.1:11434")
        self.configure(f"[llm]\nmodel = {json.dumps(model)}\nbase_url = {json.dumps(base_url)}\n")
        artwork = self.artwork(
            "live", (1800, 1200), title="Coastal Study", artist="Unknown",
            museum_description="A pale sun hangs above a blue sea and a dark foreground.",
        )
        with Image.open(artwork.cached_image_path) as source:
            image = source.copy()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 700, 1800, 1200), fill=(35, 80, 110))
        draw.ellipse((760, 120, 1040, 400), fill=(235, 205, 135))
        image.save(artwork.cached_image_path)
        with self.database() as db:
            db.upsert_artwork(artwork)

        self.cli("curate", "--count", "1")
        with self.database() as db:
            (composition,) = db.list_compositions()
        self.assertEqual(composition.artwork_ids, [artwork.id])
        self.assertNotIn("Local model unavailable or invalid", composition.decision.curator_reason)
