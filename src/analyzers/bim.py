"""IFC analyzer — element type counts via IfcOpenShell."""
from __future__ import annotations
from pathlib import Path
from collections import Counter


def analyze_ifc(path: Path) -> dict:
    import ifcopenshell
    f = ifcopenshell.open(str(path))
    schema = f.schema
    project = f.by_type("IfcProject")
    proj_name = project[0].Name if project else None

    # Element type histogram
    types_of_interest = [
        "IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcRoof", "IfcDoor",
        "IfcWindow", "IfcColumn", "IfcBeam", "IfcCovering", "IfcFurniture",
        "IfcStair", "IfcRailing", "IfcSpace", "IfcBuilding", "IfcBuildingStorey",
    ]
    counts = Counter()
    for t in types_of_interest:
        try:
            counts[t] = len(f.by_type(t))
        except Exception:
            counts[t] = 0
    counts = {k: v for k, v in counts.items() if v > 0}

    storeys = [s.Name for s in f.by_type("IfcBuildingStorey")] if "IfcBuildingStorey" in [t for t in types_of_interest] else []

    # Materials
    materials = []
    try:
        for m in f.by_type("IfcMaterial")[:50]:
            if hasattr(m, "Name") and m.Name:
                materials.append(m.Name)
    except Exception:
        pass

    return {
        "format": "ifc",
        "summary": f"IFC {schema}: project={proj_name!r}, "
                   + ", ".join(f"{k.replace('Ifc','')}={v}" for k, v in counts.items()),
        "schema": schema,
        "project_name": proj_name,
        "element_counts": counts,
        "storeys": storeys,
        "materials": materials,
    }
