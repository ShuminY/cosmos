"""IFC analyzer — element type counts via IfcOpenShell."""
from __future__ import annotations
from pathlib import Path
from collections import Counter


PREVIEW_ELEMENT_TYPES = [
    "IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcRoof", "IfcDoor",
    "IfcWindow", "IfcColumn", "IfcBeam", "IfcStair", "IfcRailing",
    "IfcCovering", "IfcCurtainWall", "IfcMember", "IfcPlate", "IfcFurniture",
]


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


def extract_ifc_preview_mesh(
    path: Path,
    *,
    max_elements: int = 80,
    max_faces: int = 50_000,
    element_types: list[str] | None = None,
) -> dict:
    import ifcopenshell
    import ifcopenshell.geom

    try:
        f = ifcopenshell.open(str(path))
    except Exception as exc:
        return {"status": "failed", "message": f"Cannot open IFC: {exc}"}

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    types = element_types or PREVIEW_ELEMENT_TYPES
    groups: dict[str, dict] = {}
    sampled = 0
    skipped = 0
    failed = 0
    failed_details = []
    total_faces = 0
    truncated = False

    for ifc_type in types:
        if sampled >= max_elements or total_faces >= max_faces:
            truncated = True
            break
        try:
            elements = f.by_type(ifc_type)
        except Exception:
            continue
        for elem in elements:
            if sampled >= max_elements or total_faces >= max_faces:
                truncated = True
                break
            try:
                shape = ifcopenshell.geom.create_shape(settings, elem)
            except Exception as exc:
                failed += 1
                if len(failed_details) < 20:
                    failed_details.append({
                        "type": ifc_type,
                        "global_id": getattr(elem, "GlobalId", None),
                        "name": getattr(elem, "Name", None),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                continue
            verts = shape.geometry.verts
            faces = shape.geometry.faces
            if not verts or not faces:
                skipped += 1
                continue

            n_faces = len(faces) // 3
            if total_faces + n_faces > max_faces:
                truncated = True
                break

            if ifc_type not in groups:
                groups[ifc_type] = {
                    "x": [], "y": [], "z": [],
                    "i": [], "j": [], "k": [],
                    "elements": 0, "faces": 0,
                }
            g = groups[ifc_type]
            offset = len(g["x"])
            for vi in range(0, len(verts), 3):
                g["x"].append(verts[vi])
                g["y"].append(verts[vi + 1])
                g["z"].append(verts[vi + 2])
            for fi in range(0, len(faces), 3):
                g["i"].append(faces[fi] + offset)
                g["j"].append(faces[fi + 1] + offset)
                g["k"].append(faces[fi + 2] + offset)
            g["elements"] += 1
            g["faces"] += n_faces
            total_faces += n_faces
            sampled += 1

    if not groups:
        return {
            "status": "empty",
            "message": "No displayable geometry found in sampled IFC elements.",
            "sampled_elements": sampled,
            "failed_elements": failed,
            "failed_details": failed_details,
            "skipped_elements": skipped,
        }

    return {
        "status": "ok",
        "groups": groups,
        "sampled_elements": sampled,
        "failed_elements": failed,
        "failed_details": failed_details,
        "skipped_elements": skipped,
        "truncated": truncated,
        "total_faces": total_faces,
    }
