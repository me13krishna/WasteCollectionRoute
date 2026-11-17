import pytest
import datetime
from backend.services.traffic_service import traffic_engine, point_to_segment_distance_m

def test_time_of_day_congestion():
    # Morning peak 9:00 AM
    morning = datetime.datetime(2026, 11, 17, 9, 0)
    factor, name, desc = traffic_engine.get_time_of_day_congestion_factor(morning)
    assert factor == 1.75
    assert "Morning" in name

    # Night 2:00 AM
    night = datetime.datetime(2026, 11, 17, 2, 0)
    factor_night, name_night, _ = traffic_engine.get_time_of_day_congestion_factor(night)
    assert factor_night == 1.00
    assert "Night" in name_night

def test_road_closure_penalty():
    coords = [
        (18.5284, 73.8480), # Depot
        (18.5300, 73.8500), # Stop A
    ]
    dist_matrix = [[0.0, 1000.0], [1000.0, 0.0]]
    dur_matrix = [[0.0, 120.0], [120.0, 0.0]]

    # Normal traffic
    res_normal = traffic_engine.apply_traffic_to_matrices(coords, dist_matrix, dur_matrix, incidents=[], traffic_enabled=True)
    dur_normal = res_normal["traffic_duration_matrix"][0][1]

    # Road closure placed right in the middle
    mid_lat = (18.5284 + 18.5300) / 2.0
    mid_lon = (73.8480 + 73.8500) / 2.0
    incidents = [
        {"incident_type": "road_closure", "lat": mid_lat, "lon": mid_lon, "active": True}
    ]
    res_closed = traffic_engine.apply_traffic_to_matrices(coords, dist_matrix, dur_matrix, incidents=incidents, traffic_enabled=True)
    dur_closed = res_closed["traffic_duration_matrix"][0][1]

    # Duration should be significantly penalized
    assert dur_closed > dur_normal * 10
    # Distance should also be penalized
    assert res_closed["traffic_distance_matrix"][0][1] > dist_matrix[0][1]
