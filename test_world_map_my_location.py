"""The map's "center on my location" button."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from world_map import WorldMapWidget


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def world_map(app):
    widget = WorldMapWidget()
    widget.resize(900, 600)
    widget.show()
    yield widget
    widget.shutdown_tile_fetcher()
    widget.close()


def _content_point_at_view_center(world_map):
    content, _w, _h = world_map._widget_pos_to_content(QPointF(world_map.width() / 2.0, world_map.height() / 2.0))
    return content.x(), content.y()


def _operator_content_point(world_map):
    return world_map._lonlat_to_xy(world_map._operator_lat, world_map._operator_lon, world_map.width(), world_map.height())


def test_button_is_disabled_until_a_location_is_known(world_map):
    assert not world_map.my_location_button.isEnabled()
    assert "Profile" in world_map.my_location_button.toolTip()
    world_map.set_operator_location(38.2, -97.5, "home")
    assert world_map.my_location_button.isEnabled()
    assert "Profile" not in world_map.my_location_button.toolTip()


def test_clicking_it_recenters_on_the_operator_after_zooming_and_panning_away(world_map):
    world_map.set_operator_location(38.2, -97.5, "home")
    world_map.center_on(-33.0, 151.0)                         # somewhere else entirely (zoomed in)
    world_map._zoom_at(QPointF(100, 100), 3.0)
    far = _content_point_at_view_center(world_map)
    target = _operator_content_point(world_map)
    assert abs(far[0] - target[0]) > 50                       # really is elsewhere

    world_map.my_location_button.click()
    cx, cy = _content_point_at_view_center(world_map)
    tx, ty = _operator_content_point(world_map)
    assert abs(cx - tx) < 1.5 and abs(cy - ty) < 1.5


def test_recentering_from_the_whole_world_view_zooms_in_to_a_useful_level(world_map):
    world_map.set_operator_location(38.2, -97.5, "home")
    assert world_map._zoom == world_map.MIN_ZOOM
    world_map.my_location_button.click()
    assert world_map._zoom >= world_map._CENTER_ON_MIN_ZOOM    # otherwise there would be nothing visible to recenter


def test_recentering_keeps_a_deeper_zoom_the_user_already_chose(world_map):
    world_map.set_operator_location(38.2, -97.5, "home")
    world_map._zoom = 120.0
    world_map.my_location_button.click()
    assert world_map._zoom == 120.0


def test_clicking_does_nothing_without_a_location(world_map):
    before = (world_map._zoom, world_map._pan_x, world_map._pan_y)
    world_map.center_on_operator()
    assert (world_map._zoom, world_map._pan_x, world_map._pan_y) == before


def test_button_sits_under_the_other_overlay_buttons_without_overlapping(world_map, app):
    app.processEvents()
    zoom_in, zoom_out, sat, me = (world_map.zoom_in_button, world_map.zoom_out_button,
                                  world_map.satellite_layer_button, world_map.my_location_button)
    assert me.x() == zoom_in.x()
    assert me.y() >= sat.y() + sat.height()
    assert not me.geometry().intersects(sat.geometry())
