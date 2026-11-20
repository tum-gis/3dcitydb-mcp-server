"""The spatial function list in the database prompt (get_spatial_capabilities).

The model picks functions by their description, so the list must offer a 2D
alpha shape next to the 3D alpha wrapping, and must not advertise SFCGAL
functions that fail on this server (they are probed on a tiny geometry).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools import dynamic_tools  # noqa: E402


class FakeDb:
    """Answers postgis_sfcgal_version() and the probes; `failing` probes raise
    like PostGIS does for a function its SFCGAL build lacks."""

    def __init__(self, failing=()):
        self.failing = failing

    def execute_single(self, query, params=None):
        if "postgis_sfcgal_version" in query:
            return {"postgis_sfcgal_version": "2.3.0"}
        for name in self.failing:
            if name in query:
                raise RuntimeError(f"{name}: requires SFCGAL 2.1.0+")
        return {"ok": True}


def _sfcgal_text(db) -> str:
    return "\n".join(dynamic_tools.get_spatial_capabilities(db)["sfcgal_functions"])


def test_2d_alpha_shape_is_offered_next_to_the_3d_wrapping():
    text = _sfcgal_text(FakeDb())
    assert "CG_OptimalAlphaShape" in text and "2D 'alpha wrapping'" in text
    assert "ST_ConcaveHull" in text
    assert "CG_3DConvexHull" in text
    assert "CG_3DAlphaWrapping(geom" in text and "NEVER for a 2D request" in text


def test_a_failing_3d_alpha_wrapping_is_not_advertised():
    """This server: PostGIS compiled against SFCGAL 2.0, CG_3DAlphaWrapping errors."""
    text = _sfcgal_text(FakeDb(failing=("CG_3DAlphaWrapping",)))
    assert "CG_3DAlphaWrapping(geom" not in text
    assert "CG_3DAlphaWrapping (the 3D alpha wrapping only) is NOT available" in text
    assert "convex hull" in text and "This does not affect 2D" in text
    assert "CG_OptimalAlphaShape" in text  # the 2D one stays


def test_extrude_of_an_area_failing_gives_the_workaround():
    text = _sfcgal_text(FakeDb(failing=("CG_Extrude('POLYGON",)))
    assert "CG_Extrude works on lines only on this server" in text
    assert "works on lines only" not in _sfcgal_text(FakeDb())
