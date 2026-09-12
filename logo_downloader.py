"""
Logo loader for F1 Scoreboard Plugin

Handles loading, caching, and resizing of F1 team logos, the F1 brand logo,
and circuit layout images. All assets are bundled as static PNGs.
Falls back to generating text-based placeholder logos for any missing teams.
"""

import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from team_colors import get_team_color, normalize_constructor_id

logger = logging.getLogger(__name__)

# f1-live: team logos cleaned up for a 1:1 LED panel (asked for 2026-09-12 with
# a photo: the green Sauber K read, the wordmark under it was mush). Chosen from
# LED-dot mock-ups; each rule is keyed on the PNG's file name, from the bundled
# 96x96 art:
#   - every logo: crop the transparent margin before shrinking, so the mark
#     fills its slot (Aston Martin's is 80x18 of the 96: a few px tall uncropped)
#   - sauber, which the 2026 Audi team also resolves to: only the green K; the
#     SAUBER wordmark and the thin ring round it are illegible at 24 px
#   - ferrari, haas: drawn on their own white tile, a white block on the panel;
#     flood the tile away from its edge so the shield / H circle stands alone
#   - then harden: sharpen as it shows over black, lift saturation a touch, and
#     drop the faint half-transparent fringe instead of drawing it dim
#   - cadillac: pixel art drawn for the panel at the row's 20 px -- the colour
#     crest the user picked (tools/f1_cadillac_logo.py, 2026-09-12) in place of
#     a thin "CAD" placeholder -- so cropping or hardening would only damage it
_KEEP_ONLY = {"sauber": lambda r, g, b: g > 120 and r < 120 and b < 120}
_TILE_SEEDS = {"ferrari": ((2, 48), (93, 48)), "haas": ((14, 48), (81, 48))}
_NO_HARDEN = {"cadillac"}


def _harden(img: Image.Image) -> Image.Image:
    """Sharpen, saturate a touch, and make every pixel fully on or off."""
    r, g, b, a = img.convert("RGBA").split()
    comp = Image.composite(Image.merge("RGB", (r, g, b)), Image.new("RGB", img.size), a)
    comp = comp.filter(ImageFilter.UnsharpMask(radius=0.8, percent=130, threshold=1))
    comp = ImageEnhance.Color(comp).enhance(1.2)
    a = a.point(lambda v: 255 if v >= 100 else 0)
    return Image.merge("RGBA", (*comp.split(), a))


def _prepare_logo(path: Path, max_width: int, max_height: int) -> Image.Image:
    """Open a team logo and fit it to max_width x max_height for the panel."""
    img = Image.open(path).convert("RGBA")
    stem = path.stem
    if stem in _NO_HARDEN:
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        return img
    for x, y in _TILE_SEEDS.get(stem, ()):
        if 0 <= x < img.width and 0 <= y < img.height and img.getpixel((x, y))[3]:
            ImageDraw.floodfill(img, (x, y), (0, 0, 0, 0), thresh=100)
    keep = _KEEP_ONLY.get(stem)
    if keep:
        px = img.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b, a = px[x, y]
                if a and not keep(r, g, b):
                    px[x, y] = (0, 0, 0, 0)
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return _harden(img)



# Map ESPN circuit names/cities to our bundled circuit image filenames
# Keys are lowercased substrings matched against circuit_name or city
CIRCUIT_FILENAME_MAP = {
    "melbourne": "melbourne",
    "albert park": "melbourne",
    "shanghai": "shanghai",
    "suzuka": "suzuka",
    "bahrain": "bahrain",
    "sakhir": "bahrain",
    "jeddah": "jeddah",
    "miami": "miami",
    "hard rock": "miami",
    "gilles villeneuve": "montreal",
    "montreal": "montreal",
    "monaco": "monaco",
    "monte carlo": "monaco",
    "catalunya": "barcelona",
    "barcelona": "barcelona",
    "red bull ring": "spielberg",
    "spielberg": "spielberg",
    "silverstone": "silverstone",
    "spa": "spa",
    "francorchamps": "spa",
    "stavelot": "spa",
    "hungaroring": "budapest",
    "budapest": "budapest",
    "zandvoort": "zandvoort",
    "monza": "monza",
    "madrid": "madrid",
    "baku": "baku",
    "marina bay": "singapore",
    "singapore": "singapore",
    "americas": "austin",
    "austin": "austin",
    "hermanos rodriguez": "mexico_city",
    "mexico": "mexico_city",
    "interlagos": "interlagos",
    "carlos pace": "interlagos",
    "sao paulo": "interlagos",
    "las vegas": "las_vegas",
    "losail": "losail",
    "lusail": "losail",
    "qatar": "losail",
    "yas marina": "yas_marina",
    "abu dhabi": "yas_marina",
}


class F1LogoLoader:
    """Loads, caches, and resizes F1 team logos and circuit images."""

    def __init__(self, plugin_dir: str = None):
        """
        Initialize the logo loader.

        Args:
            plugin_dir: Path to the plugin directory (contains assets/f1/)
        """
        if plugin_dir is None:
            plugin_dir = os.path.dirname(os.path.abspath(__file__))

        self.plugin_dir = Path(plugin_dir)
        self.teams_dir = self.plugin_dir / "assets" / "f1" / "teams"
        self.circuits_dir = self.plugin_dir / "assets" / "f1" / "circuits"
        self.f1_logo_path = self.plugin_dir / "assets" / "f1" / "f1_logo.png"

        # In-memory cache: key -> PIL Image (already resized)
        self._cache: Dict[str, Image.Image] = {}

    def get_team_logo(self, constructor_id: str, max_height: int = 28,
                      max_width: int = 28) -> Image.Image:
        """
        Get a team logo, resized to fit within max dimensions.

        Always returns an image — generates a text placeholder if no
        logo file exists for the given constructor.

        Args:
            constructor_id: Constructor identifier (any format)
            max_height: Maximum height in pixels
            max_width: Maximum width in pixels

        Returns:
            PIL Image in RGBA mode
        """
        normalized = normalize_constructor_id(constructor_id)
        cache_key = f"team_{normalized}_{max_width}x{max_height}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        logo = self._load_logo(normalized, max_width, max_height)
        self._cache[cache_key] = logo
        return logo

    def get_f1_logo(self, max_height: int = 12,
                    max_width: int = 20) -> Optional[Image.Image]:
        """
        Get the F1 brand logo.

        Args:
            max_height: Maximum height in pixels
            max_width: Maximum width in pixels

        Returns:
            PIL Image in RGBA mode, or None if unavailable
        """
        cache_key = f"f1_logo_{max_width}x{max_height}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        if self.f1_logo_path.exists():
            try:
                img = Image.open(self.f1_logo_path).convert("RGBA")
                img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
                self._cache[cache_key] = img
                return img
            except Exception as e:
                logger.warning("Failed to load F1 logo: %s", e)

        # Create F1 text placeholder
        placeholder = self._create_text_placeholder("F1", max_width, max_height,
                                                     color=(229, 0, 0))
        self._cache[cache_key] = placeholder
        return placeholder

    def _load_logo(self, constructor_id: str, max_width: int,
                   max_height: int) -> Image.Image:
        """Load a team logo from disk, with placeholder fallback.

        Always returns an image — generates a text placeholder if no
        logo file exists on disk.
        """
        logo_path = self.teams_dir / f"{constructor_id}.png"

        if logo_path.exists():
            try:
                return _prepare_logo(logo_path, max_width, max_height)
            except Exception as e:
                logger.warning("Failed to load logo for %s: %s",
                             constructor_id, e)

        # Try common filename variations
        for variation in [constructor_id.replace("_", ""),
                         constructor_id.replace("_", "-")]:
            alt_path = self.teams_dir / f"{variation}.png"
            if alt_path.exists():
                try:
                    return _prepare_logo(alt_path, max_width, max_height)
                except Exception as e:
                    logger.debug("Failed to load logo variant %s: %s",
                                 alt_path, e)

        # Create placeholder with team color
        color = get_team_color(constructor_id)
        abbr = constructor_id[:3].upper() if constructor_id else "???"
        return self._create_text_placeholder(abbr, max_width, max_height,
                                              color=color)

    def _create_text_placeholder(self, text: str, width: int, height: int,
                                  color: Tuple[int, int, int] = (200, 200, 200)
                                  ) -> Image.Image:
        """Create a simple text-based placeholder logo."""
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("assets/fonts/4x6-font.ttf", 6)
        except Exception:
            try:
                font = ImageFont.truetype(
                    str(Path(__file__).parent.parent.parent /
                        "assets" / "fonts" / "4x6-font.ttf"), 6)
            except Exception:
                font = ImageFont.load_default()

        text = text[:3]
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        x = (width - text_w) // 2
        y = (height - text_h) // 2

        # Draw outline
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=color)

        return img

    def get_circuit_image(self, circuit_name: str = "", city: str = "",
                          max_height: int = 28,
                          max_width: int = 40) -> Optional[Image.Image]:
        """
        Get a circuit layout image by matching circuit name or city.

        Args:
            circuit_name: Circuit name (e.g., "Silverstone Circuit")
            city: City name (e.g., "Melbourne")
            max_height: Maximum height in pixels
            max_width: Maximum width in pixels

        Returns:
            PIL Image in RGBA mode (white outline on transparent), or None
        """
        filename = self._resolve_circuit_filename(circuit_name, city)
        if not filename:
            return None

        cache_key = f"circuit_{filename}_{max_width}x{max_height}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        circuit_path = self.circuits_dir / f"{filename}.png"
        if not circuit_path.exists():
            return None

        try:
            img = Image.open(circuit_path).convert("RGBA")
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            self._cache[cache_key] = img
            return img
        except Exception as e:
            logger.warning("Failed to load circuit image %s: %s", filename, e)
            return None

    @staticmethod
    def _resolve_circuit_filename(circuit_name: str, city: str) -> str:
        """Resolve a circuit name/city to a filename key.

        Matches longest keys first to prevent short-key false positives
        (e.g. 'spa' matching inside a longer unrelated string).
        """
        combined = f"{circuit_name} {city}".lower()
        # Sort by key length descending so longer, more specific keys match first
        for key, filename in sorted(CIRCUIT_FILENAME_MAP.items(),
                                     key=lambda kv: len(kv[0]),
                                     reverse=True):
            if key in combined:
                return filename
        return ""

    def clear_cache(self):
        """Clear the in-memory logo cache."""
        self._cache.clear()

    def preload_all_teams(self, max_height: int = 28, max_width: int = 28):
        """
        Preload all team logos into cache.

        Args:
            max_height: Maximum height for cached logos
            max_width: Maximum width for cached logos
        """
        if not self.teams_dir.exists():
            logger.warning("Teams logo directory not found: %s", self.teams_dir)
            return

        count = 0
        for logo_file in self.teams_dir.glob("*.png"):
            constructor_id = logo_file.stem
            self.get_team_logo(constructor_id, max_height, max_width)
            count += 1

        logger.info("Preloaded %d team logos", count)
