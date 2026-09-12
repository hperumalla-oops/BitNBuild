"""Pydantic models for extracted rows, validated before insert into Supabase."""

from typing import Literal, Optional

from pydantic import BaseModel


class PermittedUseRow(BaseModel):
    zone: str
    use_type: str
    use_category: Optional[str] = None
    permission: Literal["permitted", "ancillary", "no"]
    conditions: Optional[str] = None
    source_table: Optional[str] = None
    source_page: Optional[int] = None


class FarRuleRow(BaseModel):
    zone: str
    ring: str
    road_width_min_m: Optional[float] = None
    road_width_max_m: Optional[float] = None
    far: float
    premium_far: Optional[float] = None
    premium_conditions: Optional[str] = None
    source_page: Optional[int] = None


class SetbackRuleRow(BaseModel):
    building_height_min_m: Optional[float] = None
    building_height_max_m: Optional[float] = None
    plot_area_min_sqm: Optional[float] = None
    plot_area_max_sqm: Optional[float] = None
    front_setback_m: Optional[float] = None
    rear_setback_m: Optional[float] = None
    side1_setback_m: Optional[float] = None
    side2_setback_m: Optional[float] = None
    source: Optional[str] = None
    source_page: Optional[int] = None


class HeightRuleRow(BaseModel):
    zone: str
    ring: str
    road_width_min_m: Optional[float] = None
    road_width_max_m: Optional[float] = None
    max_height_m: Optional[float] = None
    max_floors: Optional[int] = None
    max_floors_description: Optional[str] = None
    source_page: Optional[int] = None


class ParkingRuleRow(BaseModel):
    use_type: str
    unit: Optional[str] = None
    ecs_required: Optional[float] = None
    visitor_parking_pct: Optional[float] = None
    source_page: Optional[int] = None


class CoverageRuleRow(BaseModel):
    zone: str
    ring: str
    max_coverage_pct: Optional[float] = None
    source_page: Optional[int] = None


class PlotSizeRuleRow(BaseModel):
    zone: str
    ring: str
    min_plot_size_sqm: Optional[float] = None
    source_page: Optional[int] = None


class DensityRuleRow(BaseModel):
    zone: str
    ring: str
    max_dwelling_units_per_ha: Optional[float] = None
    source_page: Optional[int] = None


TABLE_ROW_MODELS = {
    "permitted_uses": PermittedUseRow,
    "far_rules": FarRuleRow,
    "setback_rules": SetbackRuleRow,
    "height_rules": HeightRuleRow,
    "parking_rules": ParkingRuleRow,
    "coverage_rules": CoverageRuleRow,
    "plot_size_rules": PlotSizeRuleRow,
    "density_rules": DensityRuleRow,
    # Amendment extractions land in the same base table, distinguished
    # by source_id / the setback_rules.source column.
    "height_rules_amendment": HeightRuleRow,
    "setback_rules_amendment": SetbackRuleRow,
}

# Maps a tables_to_extract entry to the physical table it's written to.
TABLE_NAME_ALIASES = {
    "height_rules_amendment": "height_rules",
    "setback_rules_amendment": "setback_rules",
}


class RagChunk(BaseModel):
    chunk_id: str
    source_id: str
    source_url: Optional[str] = None
    section: str
    clause_number: Optional[str] = None
    clause_title: Optional[str] = None
    text: str
    page_number: Optional[int] = None
    char_offset_start: Optional[int] = None
    char_offset_end: Optional[int] = None
    is_synthetic: bool = False
