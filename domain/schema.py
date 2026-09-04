"""
Pydantic schema for EPD extraction.

This replaces the "please output JSON shaped like this" half of
estructuracion/system_prompt.txt. Every Field(description=...) below is text the
model actually sees -- LangChain converts this whole module into a JSON Schema and
hands it to OpenAI, which then *cannot* return a differently-shaped object.

Design constraint worth knowing: OpenAI strict structured output forbids
open-ended dicts. The original prompt used dynamic keys ("data_Knauf KON13",
"kg - m2", "corr"). Those become lists-with-a-name-field here.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Enums: these become JSON Schema `enum`, so the model literally cannot
# return a value outside the list. Cheaper and stronger than prompting for it.
# --------------------------------------------------------------------------

class Quality(str, Enum):
    AUTO_DECLARED = "auto declared"
    THIRD_PARTY = "third party"


class C2CLevel(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Leaf objects
# --------------------------------------------------------------------------

class Component(BaseModel):
    """One material in the product composition (packaging excluded)."""
    name: str = Field(description="Material name, translated to English.")
    percentage: Optional[float] = Field(
        None, ge=0, le=100,
        description=(
            "Share of total product weight, in percent. If the EPD gives kg, "
            "convert to a percentage. For a range (e.g. 50-70), use the midpoint. "
            "For '<1000' or '>1000', use the edge value."
        ),
    )


class RecycledContent(BaseModel):
    """Content-information board values, per material. Must mirror `comp` names."""
    name: str = Field(description="Material name; must match a name used in comp.")
    perc_pre: Optional[float] = Field(None, ge=0, le=100, description="Pre-consumer recycled content, %.")
    perc_post: Optional[float] = Field(None, ge=0, le=100, description="Post-consumer recycled content, %.")
    perc_renew: Optional[float] = Field(
        None, ge=0, le=100,
        description="Rapidly renewable content (may appear as FSC/PEFC), %. NOT the same as biogenic.",
    )
    perc_bio: Optional[float] = Field(
        None, ge=0, le=100,
        description="Biogenic content, %. NOT the same as rapidly renewable; judge independently.",
    )


class Circularity(BaseModel):
    """
    Circular sourcing + end-of-life routes, per material.

    Use name='product' ONLY when the EPD gives no material-specific breakdown.
    All fields except `orig` should sum to 100 for each material.
    """
    name: str = Field(description="Material name matching comp, or 'product' if only product-level data exists.")
    orig: Optional[float] = Field(None, ge=0, le=100, description="Share from circular/recycled sources, %. Independent of the end-of-life fields.")
    reuse: Optional[float] = Field(None, ge=0, le=100, description="Directly reused without significant processing, %.")
    comp: Optional[float] = Field(None, ge=0, le=100, description="Sent to composting / biological treatment, %.")
    recycl: Optional[float] = Field(None, ge=0, le=100, description="Recycled into raw materials, excluding WEEE, %.")
    WEEErecycl: Optional[float] = Field(None, ge=0, le=100, description="Recycled via WEEE-specific processes, %.")
    backfill: Optional[float] = Field(None, ge=0, le=100, description="Recovered as backfill or aggregate, %.")
    refurb: Optional[float] = Field(None, ge=0, le=100, description="Recovered via refurbishment/reconditioning, %.")
    incin: Optional[float] = Field(None, ge=0, le=100, description="Incinerated, with or without energy recovery, %.")
    landfill: Optional[float] = Field(None, ge=0, le=100, description="Inert or non-hazardous landfill, %.")
    hazard: Optional[float] = Field(None, ge=0, le=100, description="Hazardous waste disposal, %.")
    nonrecov: Optional[float] = Field(None, ge=0, le=100, description="No declared recovery route / non-recoverable, %.")
    takebackrecycl: Optional[float] = Field(None, ge=0, le=100, description="Returned to manufacturer for recycling, %.")
    unknown: Optional[float] = Field(
        None, ge=0, le=100,
        description="End-of-life not specified in the EPD, %. Missing data -- NOT confirmed non-recovery.",
    )


class ProductIntegrity(BaseModel):
    comp: list[Component] = Field(default_factory=list, description="Material composition, packaging excluded.")
    rec: list[RecycledContent] = Field(default_factory=list, description="Recycled/renewable content per material.")
    circ: list[Circularity] = Field(default_factory=list, description="Circularity and end-of-life per material.")


class ConversionRatio(BaseModel):
    """
    One explicit conversion factor stated in the EPD.

    Replaces the old dynamic-key dict ("kg - m2": 4). NEVER calculate these --
    only record factors the EPD states outright.

    The field names encode the direction on purpose. An earlier version used
    from_unit/to_unit/value with the direction explained in prose, and the model
    returned the reciprocal (0.11 instead of 9.5) on some runs. A model reads a
    field NAME far more reliably than a sentence describing it: if a field can be
    read two ways, name it so it cannot.
    """
    measured_unit: str = Field(description="The unit being measured OUT, e.g. 'kg'.")
    per_unit: str = Field(description="The unit measured PER, e.g. 'm2', 'l', 'item', 'piece', 'm3'.")
    measured_units_per_one_per_unit: float = Field(
        description=(
            "How many measured_unit there are in ONE per_unit. "
            "Example: an EPD stating 'product weight 9.5 kg per m2' gives "
            "measured_unit='kg', per_unit='m2', measured_units_per_one_per_unit=9.5 "
            "-- NOT 0.105. The number is almost always the figure printed in the EPD, "
            "copied as-is; if you find yourself dividing, you have inverted it."
        )
    )
    variant: Optional[str] = Field(
        None,
        description="Variant this factor belongs to, matching a name in `variants`. Null if it applies to the whole EPD.",
    )


class CorrectionFactor(BaseModel):
    """A multiplier relating a variant back to the reference variant."""
    variant: str = Field(description="Variant name, matching a name in `variants`.")
    factor: float = Field(description="Multiplier applied to the reference variant's declared values.")


class Variant(BaseModel):
    name: str = Field(description="Exact commercial name of the variant as written in the EPD.")
    worst_case: Optional[bool] = Field(
        None,
        description=(
            "True ONLY for a combined worst-case table covering several variants at once "
            "(one generic table, no per-variant tables and no correction factors). "
            "Otherwise leave null."
        ),
    )


class EnvironmentalImpacts(BaseModel):
    """A1-A3 impacts. Never aggregate across stages; take A1-A3 as declared."""
    gwp_total: Optional[float] = Field(None, description="Total GWP, kg CO2-eq, A1-A3. May appear as 'climate change' or 'CO2 emissions'.")
    gwp_fossil: Optional[float] = Field(None, description="Fossil GWP, kg CO2-eq, A1-A3.")
    gwp_luluc: Optional[float] = Field(None, description="Land use / land use change GWP, kg CO2-eq, A1-A3.")
    gwp_bio: Optional[float] = Field(None, description="Biogenic GWP, kg CO2-eq, A1-A3.")
    fw_use: Optional[float] = Field(None, description="Total freshwater use, m3, A1-A3. NOT water deprivation potential.")
    wdp: Optional[float] = Field(None, description="Water deprivation potential, A1-A3.")


class VariantImpacts(BaseModel):
    """
    Per-variant impact table. Replaces the old "data_<variant name>" dynamic keys.

    Populate ONLY when the EPD prints a separate impacts table per variant.
    If the EPD gives only correction factors, use `correction_factors` instead.
    """
    variant: str = Field(description="Variant name, matching a name in `variants`.")
    impacts: EnvironmentalImpacts


# --------------------------------------------------------------------------
# Root object -- this is what with_structured_output() returns
# --------------------------------------------------------------------------

class EPDProduct(BaseModel):
    """Structured record extracted from a single Environmental Product Declaration."""

    product_id: int = Field(description="Copy the product id given alongside the EPD text.")

    flag: int = Field(
        ge=0, le=1,
        description=(
            "0 = a single product/variant, or a worst-case-only EPD with no conversion factors "
            "or alternative data. 1 = multiple products/variants, or a worst case accompanied by "
            "conversion factors, or separate impact tables per subproduct."
        ),
    )

    prod_name: Optional[str] = Field(None, description="Commercial product name, usually on the EPD cover page.")
    epd_code: Optional[str] = Field(None, description="EPD registration code/number, usually on the cover page.")
    methods_A1_A2: Optional[str] = Field(
        None,
        description=(
            "Full regulatory names, e.g. 'EN 15804:2012+A2:2019/AC:2021'. "
            "Separate several with commas."
        ),
    )
    qual: Optional[Quality] = Field(None, description="Data quality indicator, stated early in the EPD.")
    PCR: Optional[str] = Field(None, description="Product Category Rules referenced, including any sub-PCR.")
    date: Optional[str] = Field(
        None,
        description="EXPIRY date (YYYY-MM-DD): 'Valid until' / 'Validity'. NOT the publication date.",
    )
    prod_man: Optional[str] = Field(None, description="Product manufacturer.")
    prod_site: Optional[str] = Field(None, description="Production site: city and country where stated.")

    lifespan: Optional[float] = Field(None, description="Reference service life (RSL) used for the LCA, in years.")
    lifespan_exp: Optional[float] = Field(
        None,
        description="Manufacturer's expected service life, in years. If only one value exists, use it for both.",
    )

    reference_unit: Optional[str] = Field(
        None,
        description="Declared unit, e.g. m2, kg, metric ton, litre. Note '1000 kg' means a metric ton.",
    )
    thickness: Optional[float] = Field(None, description="Product thickness in metres, if applicable.")
    density: Optional[float] = Field(None, description="Density in kg/m3, if applicable.")

    conversion_ratios: list[ConversionRatio] = Field(
        default_factory=list,
        description=(
            "All conversion factors explicitly stated in the EPD. Extract, never compute. "
            "Include per-variant factors and density-linked ones where relevant."
        ),
    )
    correction_factors: list[CorrectionFactor] = Field(
        default_factory=list,
        description="Correction multipliers per variant, when the EPD gives factors instead of separate tables.",
    )
    variant_ref: Optional[str] = Field(
        None,
        description="Name of the reference variant the correction factors relate to. Only if the EPD states it.",
    )

    C2C: Optional[bool] = Field(None, description="True only on an explicit Cradle to Cradle certification.")
    C2C_lvl: Optional[C2CLevel] = Field(
        None,
        description="Certification level; 'unknown' if C2C is true but no level is stated. Null when C2C is false.",
    )

    variants: list[Variant] = Field(
        default_factory=list,
        description=(
            "EVERY variant named in the EPD -- do not list only the last one. "
            "Names must match those used in conversion_ratios / correction_factors / variant_impacts."
        ),
    )

    product_integrity: ProductIntegrity = Field(
        default_factory=ProductIntegrity,
        description="Composition, recycled content and circularity.",
    )

    impacts: EnvironmentalImpacts = Field(
        default_factory=EnvironmentalImpacts,
        description="Headline A1-A3 impacts for the EPD as declared.",
    )
    variant_impacts: list[VariantImpacts] = Field(
        default_factory=list,
        description=(
            "Per-variant impact tables. Populate ONLY where the EPD prints impacts separately "
            "per variant. Never synthesise these from correction factors."
        ),
    )
