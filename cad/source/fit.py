"""Quack Core in the Open Duck Mini's body: placement, clearances, the B2 wall cut, previews.

Run with the robots CAD environment (CadQuery, trimesh, manifold3d, VTK):

    DUCK_CAD=<duck folder> python boards/quack_core/cad/fit.py   # a Python with CadQuery and trimesh

It reads what the pipeline built (build/quack_core/fab/quack_core.step and the routed
kicad_pcb) and the duck's own geometry (the Open_Duck_Mini v2 print STLs and URDF meshes,
copied to robots/research/duck/cad/), and writes build/quack_core/cad/:

  fit.json                         every number below, measured here
  body_middle_top_usbc.stl / .step the B2 part: body_middle_top with the USB-C opening
  fit_iso.png, fit_top.png, fit_section.png, fit_usbc.png   headless VTK renders

Frames. The CAD frame is the print STLs' own (spec 1.1): +x forward, +y the robot's left,
+z up, mm. The board outline is CAD x -93.5..-42.0, y -50.5..+50.5, bottom face z -6.10 on
the four trunk_top boss tips (spec 2.7). KiCad's board frame is x right, y down from the
outline's top-left corner, which is CAD (x -42.0, y +50.5); so x_cad = -42.0 - y_kicad and
y_cad = 50.5 - x_kicad. kicad-cli's STEP keeps KiCad's absolute x and flips y (y_step =
-y_abs), with the board's bottom face at z = 0.

What is measured, and what is not:
  * the board, every component body the STEP carries, an envelope for each part whose KiCad
    model is missing from KiCad 10.0.6 (named in fit.json, heights from catalogue rows or
    marked estimated), and a Pi Zero 2 W envelope on the socket, against every body part;
  * clearance is the smallest distance from points sampled on the assembly's surface (0.25 mm
    apart) to each body mesh, and from points sampled on the body near the board to the
    assembly; a sample inside a body is an interference, reported with its depth;
  * the board's bottom face sits on the four boss tips by design: that contact is reported
    as the seating check, not as an interference;
  * THT leads under the board are reported twice: as modelled (KiCad's 3D models leave
    1.8 mm of lead below the board), and clipped to 2.0 mm, the spec's rule over the tongue.
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import logging

import numpy as np
import trimesh

logging.getLogger("trimesh").setLevel(logging.ERROR)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
from heypcb_kicad import sexpr as S  # noqa: E402  (pure Python, no pcbnew)

DUCK = Path(os.environ.get("DUCK_CAD", "duck_cad"))
KICAD_3D = Path(os.environ.get("KICAD10_3DMODEL_DIR", "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"))

# spec 2.7: outline corner (KiCad origin) in the CAD frame, and the board's bottom face
CAD_X0, CAD_Y0, Z_BOTTOM, THICK = -42.0, 50.5, -6.10, 1.6
# the Waveshare adapter bosses the board screws onto (spec 2.3), CAD frame
BOSSES = [(-84.12, 14.00), (-84.12, -14.00), (-47.00, 14.00), (-47.00, -14.00)]
# the body parts: print STLs (spec 1.1 cad/print), all in the CAD frame
BODY_PRINT = ["trunk_top", "trunk_bottom", "body_middle_top", "body_middle_bottom", "body_back",
              "battery_pack_lid", "body_front"]
# URDF meshes that stay in the body (servo cases and horns, the backpack's cells, BMS, switch);
# the Waveshare adapter ("board") and the BNO055 breakout in the trunk are what this board replaces
URDF_KEEP = re.compile(r"^(wj-wk00|drive_palonier|passive_palonier|cell|holder|bms|power_switch)")
URDF_OFFSET = np.array([19.0, 0.0, -64.89])  # CAD = trunk + offset (spec 1.1)
# Pi Zero 2 W on the socket (spec 2.7): outline, stack and components (estimated where the
# spec says so). z is above the brain board's top face.
PI = {"x": (-81.0, -51.0), "y": (-32.5, 32.5), "corner_r": 3.0, "pcb_z": (11.0, 12.6),
      "top_z": 3.45, "spacer": {"x": (-57.0, -52.0), "y": (-25.5, 25.5), "z": (8.5, 11.0)},
      "sd": {"x": (-76.0, -65.0), "y": (32.5, 35.0), "z": (12.6, 13.9)}}
# parts KiCad 10.0.6 names a STEP for but does not install: envelope heights above the board
# (mm) and lead length below it. Source per row; "estimated" where no drawing was read.
ENVELOPES = {
    "AMASS_XT30UPB-M_1x02_P5.0mm_Vertical": (11.0, 1.8, "estimated: AMASS XT30 upright PCB male, no drawing read"),
    "Molex_SPOX_5267-03A_1x03_P2.50mm_Vertical": (6.0, 1.8, "LCSC C2845815 row: 10 x 4.9 x 6 mm (HC-5264-3A)"),
    "Fuse_Littelfuse-NANO2-451_453": (2.7, 0.0, "Littelfuse NANO2 451/453 family, 6.1 x 2.69 x 2.69 mm"),
    "L_Coilcraft_XxL4020": (2.1, 0.0, "Coilcraft XFL4020: 4.0 x 4.0 x 2.1 mm maximum"),
    "USB_C_Receptacle_HRO_TYPE-C-31-M-12": (3.26, 0.0, "HRO TYPE-C-31-M-12 shell, 8.94 x 7.35 x 3.26 mm (heypcb_kicad/models3d.py; the board carries a generated VRML body, which no STEP export includes)"),
}
CLIP = 2.0  # spec 2.7: THT leads over the tongue clipped to <= 2.0 mm


# ---------------------------------------------------------------- frames
def kicad_to_cad(x, y):
    return CAD_X0 - y, CAD_Y0 - x


def step_to_cad_matrix(ox, oy, z_step_bottom):
    """x_cad = y_step + oy + CAD_X0, y_cad = CAD_Y0 + ox - x_step, z_cad = z_step - z0 + Z_BOTTOM."""
    m = np.eye(4)
    m[:3, :3] = [[0, 1, 0], [-1, 0, 0], [0, 0, 1]]
    m[:3, 3] = [oy + CAD_X0, CAD_Y0 + ox, Z_BOTTOM - z_step_bottom]
    return m


# ---------------------------------------------------------------- the board file
def read_board(pcb_path):
    root = S.load(str(pcb_path))
    xs, ys = [], []
    for g in S.find_all(root, "gr_line") + S.find_all(root, "gr_arc"):
        if str(S.find(g, "layer")[1]) != "Edge.Cuts":
            continue
        for key in ("start", "end", "mid"):
            n = S.find(g, key)
            if n is not None:
                xs.append(float(n[1]))
                ys.append(float(n[2]))
    ox, oy = min(xs), min(ys)
    fps = []
    for fp in S.find_all(root, "footprint"):
        lib = str(fp[1])
        at = S.find(fp, "at")
        ref = None
        for p in S.find_all(fp, "property"):
            if str(p[1]) == "Reference":
                ref = str(p[2])
        layer = str(S.find(fp, "layer")[1])
        x, y = float(at[1]), float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0
        crt, fab = [], []
        for kind in ("fp_rect", "fp_line", "fp_poly", "fp_circle", "fp_arc"):
            for g in S.find_all(fp, kind):
                ln = S.find(g, "layer")
                if ln is None or str(ln[1]) not in ("F.CrtYd", "B.CrtYd", "F.Fab"):
                    continue
                dest = fab if str(ln[1]) == "F.Fab" else crt
                for key in ("start", "end", "center", "mid"):
                    n = S.find(g, key)
                    if n is not None:
                        dest.append((float(n[1]), float(n[2])))
                pts = S.find(g, "pts")
                if pts is not None:
                    dest += [(float(q[1]), float(q[2])) for q in S.find_all(pts, "xy")]
        models = [str(m[1]) for m in S.find_all(fp, "model")]
        attrs = S.find(fp, "attr")
        dnp = attrs is not None and "dnp" in [str(a) for a in attrs[1:]]
        tht = any(str(p[2]) == "thru_hole" for p in S.find_all(fp, "pad"))
        fps.append({"ref": ref, "lib": lib, "x": x - ox, "y": y - oy, "rot": rot, "layer": layer,
                    "crt_local": crt, "fab_local": fab, "models": models, "dnp": dnp, "tht": tht})
    return ox, oy, fps


def courtyard_box(fp, key="crt_local"):
    """Footprint courtyard (or F.Fab, key="fab_local") bounding box in board mm, rotation applied."""
    if not fp[key]:
        return None
    a = math.radians(fp["rot"])
    pts = []
    for u, v in fp[key]:
        # KiCad: positive angles turn counter-clockwise on screen (y down)
        x = u * math.cos(a) + v * math.sin(a)
        y = -u * math.sin(a) + v * math.cos(a)
        pts.append((fp["x"] + x, fp["y"] + y))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def missing_model(fp):
    for m in fp["models"]:
        path = Path(m.replace("${KICAD10_3DMODEL_DIR}", str(KICAD_3D)))
        if path.exists() or path.with_suffix(".wrl").exists():
            return False
    return bool(fp["models"])


# ---------------------------------------------------------------- meshes
def step_meshes(step_path, tol=0.05):
    import cadquery as cq
    shape = cq.importers.importStep(str(step_path))
    out = []
    for sol in shape.solids().vals():
        verts, faces = sol.tessellate(tol, 0.3)
        if not faces:
            continue
        v = np.array([[p.x, p.y, p.z] for p in verts])
        out.append(trimesh.Trimesh(v, np.array(faces), process=True))
    return out


def box(x0, x1, y0, y1, z0, z1):
    b = trimesh.creation.box(extents=[x1 - x0, y1 - y0, z1 - z0])
    b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2])
    return b


def rounded_slab(x0, x1, y0, y1, z0, z1, r):
    import manifold3d as mf
    cs = mf.CrossSection.square([x1 - x0 - 2 * r, y1 - y0 - 2 * r]).offset(r, mf.JoinType.Round, 2.0, 48)
    m = mf.Manifold.extrude(cs, z1 - z0).translate([x0 + r, y0 + r, z0])
    mesh = m.to_mesh()
    return trimesh.Trimesh(mesh.vert_properties[:, :3], mesh.tri_verts)


def body_meshes():
    out = {}
    for name in BODY_PRINT:
        out[name] = trimesh.load(str(DUCK / "cad" / "print" / (name + ".stl")), force="mesh")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import duck_urdf as MC
    parts, _ = MC.load()
    servo = []
    for p in parts:
        if not URDF_KEEP.match(p["name"]):
            continue
        m = p["mesh"].copy()
        m.apply_translation(URDF_OFFSET)
        lo, hi = m.bounds
        # only what can come near the rear bay
        if hi[0] < -140 or lo[0] > 20 or lo[2] > 60 or hi[2] < -60:
            continue
        servo.append((p["name"], m))
    groups = {}
    for name, m in servo:
        key = "sts3215_case" if name.startswith("wj-") else ("servo_horn" if "palonier" in name else name)
        groups.setdefault(key, []).append(m)
    for key, ms in groups.items():
        out["urdf:" + key] = trimesh.util.concatenate(ms)
    return out


# ---------------------------------------------------------------- measurements
def sample(mesh, spacing):
    n = max(200, int(mesh.area / (spacing * spacing)))
    pts, _ = trimesh.sample.sample_surface_even(mesh, n, radius=spacing * 0.5, seed=1)
    return np.vstack([pts, mesh.vertices])


def crop(mesh, lo, hi):
    """The faces of a mesh whose bounding boxes touch an axis-aligned box (for sampling only)."""
    tri = mesh.triangles
    tlo, thi = tri.min(axis=1), tri.max(axis=1)
    keep = np.all((thi >= lo) & (tlo <= hi), axis=1)
    if not keep.any():
        return None
    return trimesh.Trimesh(mesh.vertices, mesh.faces[keep], process=True)


def _gap(points, mesh, near):
    """Exact signed gap (negative = inside a watertight mesh) for the points flagged `near`."""
    d = np.full(len(points), np.inf)
    if near.any():
        q = trimesh.proximity.ProximityQuery(mesh)
        _, dn, _ = q.on_surface(points[near])
        if mesh.is_watertight:
            dn = np.where(mesh.contains(points[near]), -dn, dn)
        d[near] = dn
    return d


def clearance(parts, body, lo, hi, spacing=0.25, exclude=None, approx_cut=4.0):
    """Smallest gap between the assembly `parts` [(label, mesh)] and one body mesh, measured both
    ways near the board (box lo..hi): assembly surface samples against the body, and body surface
    samples against each assembly part. A KD tree on 0.4 mm body samples picks the candidates;
    exact point-to-triangle distances decide. exclude(points) masks designed contact."""
    from scipy.spatial import cKDTree
    res = {"min_mm": None, "interference": False, "penetration_mm": 0.0}
    near_body = crop(body, lo - 15.0, hi + 15.0)
    if near_body is None:
        return {"min_mm": None, "note": "no body surface within 15 mm of the board"}
    bpts = sample(near_body, 0.4)
    tree = cKDTree(bpts)
    best = (np.inf, None, None, None)
    a_samples = []
    for label, mesh in parts:
        pts = sample(mesh, spacing)
        if exclude is not None:
            pts = pts[~exclude(pts)]
        a_samples.append(pts)
        if not len(pts):
            continue
        dk, _ = tree.query(pts, distance_upper_bound=approx_cut + 1.0)
        d = _gap(pts, body, dk < approx_cut + 0.5)
        k = int(np.argmin(d))
        if d[k] < best[0]:
            q = trimesh.proximity.ProximityQuery(body)
            cp, _, _ = q.on_surface(pts[k:k + 1])
            best = (float(d[k]), label, pts[k], cp[0])
    # body samples inside the board's box, against each part
    inbox = np.all((bpts >= lo - 1.0) & (bpts <= hi + 1.0), axis=1)
    bb = bpts[inbox]
    if exclude is not None and len(bb):
        bb = bb[~exclude(bb)]
    if len(bb):
        for (label, mesh), pts in zip(parts, a_samples):
            if not len(pts):
                continue
            t2 = cKDTree(pts)
            dk, _ = t2.query(bb, distance_upper_bound=approx_cut + 1.0)
            d = _gap(bb, mesh, dk < approx_cut + 0.5)
            k = int(np.argmin(d))
            if d[k] < best[0]:
                q = trimesh.proximity.ProximityQuery(mesh)
                cp, _, _ = q.on_surface(bb[k:k + 1])
                best = (float(d[k]), label, cp[0], bb[k])
    if np.isfinite(best[0]):
        res["min_mm"] = round(best[0], 2)
        res["nearest_part"] = best[1]
        res["at_assembly"] = [round(float(v), 2) for v in best[2]]
        res["at_body"] = [round(float(v), 2) for v in best[3]]
        if best[0] < 0:
            res["interference"] = True
            res["penetration_mm"] = round(-best[0], 2)
    else:
        res["min_mm"] = None
        res["note"] = "nothing within %.1f mm" % approx_cut
        res["min_mm_more_than"] = approx_cut
    return res


def pilot_centre(trunk_top, x, y):
    """The boss's 2.0 mm pilot, found by casting rays down onto the boss tip on a 0.05 mm grid:
    rays that pass the tip plane (z -6.10) and travel more than 2 mm further are in the pilot."""
    g = np.arange(-1.6, 1.6001, 0.05)
    xx, yy = np.meshgrid(g, g)
    o = np.c_[xx.ravel() + x, yy.ravel() + y, np.full(xx.size, Z_BOTTOM + 0.5)]
    d = np.tile([0, 0, -1.0], (len(o), 1))
    loc, ri, _ = trunk_top.ray.intersects_location(o, d, multiple_hits=False)
    depth = np.full(len(o), np.inf)
    depth[ri] = (Z_BOTTOM + 0.5) - loc[:, 2]
    deep = depth > 2.5
    tip = depth[np.isfinite(depth) & ~deep]
    if not deep.any():
        return None
    c = o[deep][:, :2].mean(axis=0)
    r = math.sqrt(deep.sum() * 0.05 * 0.05 / math.pi)
    return {"centre": [round(float(c[0]), 3), round(float(c[1]), 3)], "pilot_d_mm": round(2 * r, 2),
            "tip_z": round(float(Z_BOTTOM + 0.5 - np.median(tip)), 2) if len(tip) else None}


def free_height_above(meshes, x, y, z_from):
    o = np.array([[x, y, z_from]])
    best = np.inf
    for m in meshes:
        loc, _, _ = m.ray.intersects_location(o, np.array([[0, 0, 1.0]]), multiple_hits=False)
        if len(loc):
            best = min(best, float(loc[0, 2] - z_from))
    return None if not np.isfinite(best) else round(best, 1)


# ---------------------------------------------------------------- the B2 part
def usbc_cut(body_middle_top, cx, cz, w=13.0, h=7.5, r=1.5, y_inner=-51.0, y_outer=-56.0):
    """body_middle_top minus a rounded 13.0 x 7.5 mm opening through the right (-y) side wall,
    centred on the receptacle (spec 5, B2)."""
    import manifold3d as mf
    cs = mf.CrossSection.square([w - 2 * r, h - 2 * r]).offset(r, mf.JoinType.Round, 2.0, 48)
    # extrude along +z, then turn so the extrusion runs along -y
    tool = mf.Manifold.extrude(cs, y_inner - y_outer).translate([-(w - 2 * r) / 2, -(h - 2 * r) / 2, 0])
    tool = tool.rotate([90, 0, 0]).translate([cx, y_inner, cz])
    body = mf.Manifold(mf.Mesh(vert_properties=np.asarray(body_middle_top.vertices, np.float32),
                               tri_verts=np.asarray(body_middle_top.faces, np.uint32)))
    cut = body - tool
    mesh = cut.to_mesh()
    out = trimesh.Trimesh(mesh.vert_properties[:, :3], mesh.tri_verts)
    t = tool.to_mesh()
    return out, trimesh.Trimesh(t.vert_properties[:, :3], t.tri_verts)


def mesh_to_step(mesh, path):
    """A faceted STEP of a closed mesh (OCP sewing), for CAD tools that want STEP."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing, \
        BRepBuilderAPI_MakeSolid
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    sew = BRepBuilderAPI_Sewing(1e-3)
    v = mesh.vertices
    for f in mesh.faces:
        poly = BRepBuilderAPI_MakePolygon(gp_Pnt(*v[f[0]]), gp_Pnt(*v[f[1]]), gp_Pnt(*v[f[2]]), True)
        face = BRepBuilderAPI_MakeFace(poly.Wire(), True)
        if face.IsDone():
            sew.Add(face.Face())
    sew.Perform()
    shell = TopoDS.Shell_s(sew.SewedShape())
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    w = STEPControl_Writer()
    w.Transfer(solid, STEPControl_AsIs)
    return w.Write(str(path)) == 1


# ---------------------------------------------------------------- previews (VTK, offscreen)
def _poly(mesh):
    import vtk
    from vtk.util import numpy_support as ns
    pts = vtk.vtkPoints()
    pts.SetData(ns.numpy_to_vtk(np.ascontiguousarray(mesh.vertices, dtype=np.float64), deep=True))
    f = np.hstack([np.full((len(mesh.faces), 1), 3), mesh.faces]).astype(np.int64).ravel()
    cells = vtk.vtkCellArray()
    cells.ImportLegacyFormat(ns.numpy_to_vtkIdTypeArray(f, deep=True))
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    n = vtk.vtkPolyDataNormals()
    n.SetInputData(pd)
    n.SetFeatureAngle(35.0)
    n.Update()
    return n.GetOutput()


def render(items, path, view, size=(1600, 1000), title=None, parallel=None, clip=None):
    """items: [(mesh, rgb, opacity)]; view: (focal, position, up). clip: (origin, normal) cuts every
    item with a plane (a section view)."""
    import vtk
    ren = vtk.vtkRenderer()
    ren.SetBackground(1.0, 1.0, 1.0)
    ren.SetBackground2(0.86, 0.89, 0.93)
    ren.GradientBackgroundOn()
    for mesh, rgb, alpha in items:
        pd = _poly(mesh)
        mapper = vtk.vtkPolyDataMapper()
        if clip is not None:
            plane = vtk.vtkPlane()
            plane.SetOrigin(*clip[0])
            plane.SetNormal(*clip[1])
            mapper.AddClippingPlane(plane)
        mapper.SetInputData(pd)
        act = vtk.vtkActor()
        act.SetMapper(mapper)
        pr = act.GetProperty()
        pr.SetColor(*rgb)
        pr.SetOpacity(alpha)
        pr.SetAmbient(0.25)
        pr.SetDiffuse(0.75)
        pr.SetSpecular(0.15)
        pr.SetSpecularPower(20)
        ren.AddActor(act)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(*view[0])
    cam.SetPosition(*view[1])
    cam.SetViewUp(*view[2])
    if parallel:
        cam.ParallelProjectionOn()
        cam.SetParallelScale(parallel)
    ren.ResetCameraClippingRange()
    if not parallel:
        cam.SetViewAngle(28)
    lk = vtk.vtkLightKit()
    lk.AddLightsToRenderer(ren)
    if title:
        t = vtk.vtkTextActor()
        t.SetInput(title)
        tp = t.GetTextProperty()
        tp.SetFontSize(19)
        tp.SetColor(0.12, 0.14, 0.18)
        tp.SetFontFamilyToArial()
        t.SetDisplayPosition(24, size[1] - 44)
        ren.AddViewProp(t)
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetMultiSamples(8)
    win.AddRenderer(ren)
    win.SetSize(*size)
    win.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(win)
    w2i.Update()
    png = vtk.vtkPNGWriter()
    png.SetFileName(str(path))
    png.SetInputConnection(w2i.GetOutputPort())
    png.Write()
    return str(path)


# ---------------------------------------------------------------- main
COLOURS = {"pcb": (0.08, 0.36, 0.22), "part": (0.18, 0.18, 0.2), "connector": (0.93, 0.93, 0.9),
           "envelope": (0.95, 0.62, 0.15), "pi": (0.2, 0.5, 0.25), "pi_parts": (0.75, 0.75, 0.72),
           "body": (0.96, 0.84, 0.36), "trunk": (0.55, 0.62, 0.72), "servo": (0.25, 0.27, 0.3), "cut": (0.95, 0.35, 0.3)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", default=str(REPO / "build" / "quack_core"))
    ap.add_argument("--step", default=None, help="default: <build>/fab/quack_core.step")
    ap.add_argument("--pcb", default=None, help="default: <build>/quack_core.kicad_pcb")
    ap.add_argument("--out", default=None, help="default: <build>/cad")
    ap.add_argument("--spacing", type=float, default=0.25, help="surface sample spacing, mm")
    ap.add_argument("--no-render", action="store_true")
    a = ap.parse_args(argv)
    build = Path(a.build)
    step = Path(a.step or build / "fab" / "quack_core.step")
    pcb = Path(a.pcb or build / "quack_core.kicad_pcb")
    out = Path(a.out or build / "cad")
    out.mkdir(parents=True, exist_ok=True)
    report = {"inputs": {"step": os.path.relpath(step), "pcb": os.path.relpath(pcb), "duck": "$DUCK_CAD"},
              "frame": "CAD frame of the Open_Duck_Mini v2 print STLs: +x forward, +y left, +z up, mm (spec 1.1)"}

    # ------------------------------------------------ the board, in the CAD frame
    ox, oy, fps = read_board(pcb)
    by_ref = {f["ref"]: f for f in fps}
    solids = step_meshes(step)
    zb = min(m.bounds[0][2] for m in solids if (m.bounds[1][2] - m.bounds[0][2]) < 1.7 and m.extents[0] > 90)
    pcb_solid = [m for m in solids if m.extents[0] > 90 and m.extents[1] > 45]
    z_step_bottom = min(m.bounds[0][2] for m in pcb_solid) if pcb_solid else zb
    M = step_to_cad_matrix(ox, oy, z_step_bottom)
    parts = []
    centres = np.array([[f["x"], f["y"]] for f in fps])
    refs = [f["ref"] for f in fps]
    for m in solids:
        m = m.copy()
        is_pcb = m.extents[0] > 90 and m.extents[1] > 45
        c = m.bounds.mean(axis=0)
        bx, by = c[0] - ox, -c[1] - oy
        m.apply_transform(M)
        if is_pcb:
            parts.append(("PCB", m, "pcb"))
            continue
        k = int(np.argmin(np.hypot(centres[:, 0] - bx, centres[:, 1] - by)))
        ref = refs[k]
        kind = "connector" if ref.startswith("J") else "part"
        parts.append((ref, m, kind))
    # envelopes for parts whose KiCad model is missing (not in the STEP)
    env_rows = []
    for f in fps:
        if f["dnp"] or not missing_model(f):
            continue
        name = f["lib"].split(":", 1)[1]
        if name not in ENVELOPES:
            env_rows.append({"ref": f["ref"], "footprint": name, "envelope": None,
                             "note": "no model and no envelope: not checked"})
            continue
        h, lead, src = ENVELOPES[name]
        bb = courtyard_box(f, "fab_local") or courtyard_box(f)
        (x0, y0, x1, y1) = bb
        cx0, cy0 = kicad_to_cad(x0, y0)
        cx1, cy1 = kicad_to_cad(x1, y1)
        top = Z_BOTTOM + THICK
        env = box(min(cx0, cx1), max(cx0, cx1), min(cy0, cy1), max(cy0, cy1), top, top + h)
        parts.append((f["ref"], env, "envelope"))
        if lead > 0 and f["tht"]:
            parts.append((f["ref"] + " leads", box(min(cx0, cx1) + 0.8, max(cx0, cx1) - 0.8, min(cy0, cy1) + 0.8,
                                                   max(cy0, cy1) - 0.8, Z_BOTTOM - lead, Z_BOTTOM), "envelope"))
        env_rows.append({"ref": f["ref"], "footprint": name, "height_mm": h, "leads_below_mm": lead if f["tht"] else 0.0,
                         "footprint_box_cad": [round(min(cx0, cx1), 2), round(max(cx0, cx1), 2), round(min(cy0, cy1), 2),
                                               round(max(cy0, cy1), 2)], "source": src})
    report["envelopes"] = env_rows
    # the Pi Zero 2 W on the socket
    top = Z_BOTTOM + THICK
    pi_parts = [
        ("Pi PCB", rounded_slab(PI["x"][0], PI["x"][1], PI["y"][0], PI["y"][1], top + PI["pcb_z"][0], top + PI["pcb_z"][1],
                                PI["corner_r"]), "pi"),
        ("Pi top components (3.45 mm envelope)", rounded_slab(PI["x"][0], PI["x"][1], PI["y"][0], PI["y"][1], top + PI["pcb_z"][1],
                                                              top + PI["pcb_z"][1] + PI["top_z"], PI["corner_r"]), "pi_parts"),
        ("Pi header spacer", box(*PI["spacer"]["x"], *PI["spacer"]["y"], top + PI["spacer"]["z"][0], top + PI["spacer"]["z"][1]), "pi_parts"),
        ("Pi microSD card", box(*PI["sd"]["x"], *PI["sd"]["y"], top + PI["sd"]["z"][0], top + PI["sd"]["z"][1]), "pi_parts"),
    ]
    parts += pi_parts
    report["pi_envelope"] = {"outline_cad": {"x": PI["x"], "y": PI["y"]},
                             "pcb_z_above_board_top": PI["pcb_z"], "top_components_mm": PI["top_z"],
                             "basis": "spec 2.7: 8.5 mm socket + 2.5 mm spacer = 11 mm, 1.6 mm PCB, 3.45 mm mini-HDMI as a full-area slab "
                                      "(estimated, conservative); microSD card 2.5 mm past the +y edge (estimated)"}
    assembly = [(lab, m) for lab, m, _ in parts]
    lo = np.min([m.bounds[0] for _, m in assembly], axis=0)
    hi = np.max([m.bounds[1] for _, m in assembly], axis=0)
    pcbm = [m for lab, m, _ in parts if lab == "PCB"][0]
    report["board"] = {"bounds_cad": [[round(float(v), 2) for v in pcbm.bounds[0]], [round(float(v), 2) for v in pcbm.bounds[1]]],
                       "spec_outline": {"x": [-93.5, -42.0], "y": [-50.5, 50.5], "z_bottom": Z_BOTTOM},
                       "assembly_bounds_cad": [[round(float(v), 2) for v in lo], [round(float(v), 2) for v in hi]],
                       "step_solids": len(solids), "watertight_solids": int(sum(m.is_watertight for m in solids))}

    # ------------------------------------------------ the body
    bodies = body_meshes()
    trunk_top = bodies["trunk_top"]
    # mounting: holes vs the boss pilots
    holes = []
    for f in fps:
        if f["lib"].endswith("MountingHole_2.7mm_M2.5") and f["ref"] in ("H1", "H2", "H3", "H4"):
            cx, cy = kicad_to_cad(f["x"], f["y"])
            near = min(BOSSES, key=lambda b: math.hypot(b[0] - cx, b[1] - cy))
            pil = pilot_centre(trunk_top, near[0], near[1])
            row = {"ref": f["ref"], "hole_cad": [round(cx, 3), round(cy, 3)], "spec_boss": list(near)}
            if pil:
                row.update(pilot=pil, offset_mm=round(math.hypot(pil["centre"][0] - cx, pil["centre"][1] - cy), 3))
            holes.append(row)
    report["mounting"] = {"holes": holes, "max_offset_mm": max((h.get("offset_mm", 99) for h in holes), default=None),
                          "board_bottom_z": Z_BOTTOM,
                          "boss_tips_z": sorted({h["pilot"]["tip_z"] for h in holes if h.get("pilot")}),
                          "screw": "M2.5 x 8 thread-forming into the 2.0 mm pilots (spec B1)"}

    def seat(points):
        """Designed contact: the board seated on the four bosses. Within 3.6 mm of each boss axis, from
        the boss's foot on the tongue (3.1 mm under the board, where the cone is 6.9 mm across) to the
        board's top face: the boss cone, its tip, the hole wall over it and the board face around it."""
        m = np.zeros(len(points), bool)
        zok = (points[:, 2] > Z_BOTTOM - 3.1) & (points[:, 2] < Z_BOTTOM + THICK + 0.05)
        for bx, by in BOSSES:
            m |= zok & (np.hypot(points[:, 0] - bx, points[:, 1] - by) < 3.6)
        return m

    rows = {}
    for name, body in bodies.items():
        r = clearance(assembly, body, lo, hi, spacing=a.spacing, exclude=seat)
        rows[name] = r
        print("%-28s %s" % (name, json.dumps({k: r.get(k) for k in ("min_mm", "nearest_part", "interference")})), flush=True)
    report["clearance"] = rows
    finite = {k: v["min_mm"] for k, v in rows.items() if v.get("min_mm") is not None}
    worst = min(finite, key=finite.get) if finite else None
    report["summary"] = {"min_clearance_mm": finite.get(worst), "closest_body_part": worst,
                         "closest_board_part": rows[worst].get("nearest_part") if worst else None,
                         "interferences": sorted(k for k, v in rows.items() if v.get("interference"))}

    # below the board: the 3.0 mm over the tongue, as modelled and with leads clipped to 2.0 mm
    under = [(lab, m) for lab, m in assembly if m.bounds[0][2] < Z_BOTTOM - 0.01]
    report["below_board"] = {"parts_below_bottom_face": sorted({lab for lab, _ in under}),
                             "deepest_mm": round(float(Z_BOTTOM - min(m.bounds[0][2] for _, m in under)), 2) if under else 0.0,
                             "tongue_gap_mm": 3.0, "clip_rule_mm": CLIP}

    # free height above each connector (for the mating plug and its cable)
    fh = {}
    for f in fps:
        if not f["ref"] or not f["ref"].startswith("J") or f["dnp"]:
            continue
        cx, cy = kicad_to_cad(f["x"], f["y"])
        fh[f["ref"]] = free_height_above(list(bodies.values()), cx, cy, Z_BOTTOM + THICK)
    report["free_height_above_connectors_mm"] = fh

    # ------------------------------------------------ B2: the USB-C opening in body_middle_top
    j4 = by_ref.get("J4")
    jx, jy = kicad_to_cad(j4["x"], j4["y"])
    usb_cz = Z_BOTTOM + THICK + ENVELOPES["USB_C_Receptacle_HRO_TYPE-C-31-M-12"][0] / 2
    cut, tool = usbc_cut(bodies["body_middle_top"], jx, usb_cz)
    stl = out / "body_middle_top_usbc.stl"
    cut.export(str(stl))
    stp = out / "body_middle_top_usbc.step"
    ok = mesh_to_step(cut, stp)
    removed = bodies["body_middle_top"].volume - cut.volume
    usb_env = [m for lab, m, _ in parts if lab == "J4"][0]
    after = clearance([("J4", usb_env)], cut, lo, hi, spacing=0.2)
    mouth_y = float(usb_env.bounds[0][1])
    report["b2_usbc_opening"] = {
        "receptacle_centre_cad": [round(jx, 2), round(jy, 2), round(usb_cz, 2)],
        "spec_centre": [-62.0, None, -2.9],
        "opening_mm": [13.0, 7.5], "corner_r_mm": 1.5, "through": "y -51.0 to -56.0 (wall y -52 to -55)",
        "mouth_y_cad": round(mouth_y, 2), "wall_inner_y": -52.0, "wall_outer_y": -55.0,
        "mouth_inside_outer_face_mm": round(mouth_y - (-55.0), 2),
        "removed_volume_mm3": round(float(removed), 1), "watertight": bool(cut.is_watertight),
        "j4_clearance_to_cut_part": after, "stl": stl.name, "step": stp.name if ok else None,
        "split_plane_z": -13.78, "opening_z": [round(usb_cz - 3.75, 2), round(usb_cz + 3.75, 2)]}
    bodies_cut = dict(bodies)
    bodies_cut["body_middle_top"] = cut

    out_json = out / "fit.json"
    tmp = out_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, out_json)
    print("wrote", out_json)

    if a.no_render:
        return report
    # ------------------------------------------------ previews
    body_items = []
    for name, m in bodies_cut.items():
        if name in ("body_middle_top", "body_back", "battery_pack_lid", "body_front") or \
                name in ("urdf:cell", "urdf:holder", "urdf:bms", "urdf:power_switch"):
            continue
        col = COLOURS["trunk"] if name.startswith("trunk") else (COLOURS["servo"] if name.startswith("urdf") else COLOURS["body"])
        body_items.append((m, col, 1.0))
    board_items = [(m, COLOURS[k], 1.0 if k != "pi" else 0.55) for _, m, k in parts]
    board_items = [(m, c, 0.35 if c in (COLOURS["pi"], COLOURS["pi_parts"]) else 1.0) for m, c, _ in board_items]
    imgs = {}
    fx = (-68.0, 0.0, -2.0)
    imgs["fit_iso.png"] = render(body_items + board_items, out / "fit_iso.png",
                                 (fx, (fx[0] - 165, fx[1] - 120, fx[2] + 150), (0, 0, 1)),
                                 title="Quack Core on trunk_top's bosses (top shell, backpack and lid hidden; Pi Zero 2 W as a translucent envelope)")
    top_items = [(m, c, a_) for m, c, a_ in body_items] + [(m, c, 1.0) for m, c, _ in [(m, COLOURS[k], 1) for _, m, k in parts if k not in ("pi", "pi_parts")]]
    imgs["fit_top.png"] = render(top_items, out / "fit_top.png", ((-68.0, 0.0, 0.0), (-68.0, 0.0, 400.0), (1, 0, 0)),
                                 parallel=60, title="Top view, front up (Pi hidden): orange = envelopes for parts without a KiCad model")
    sec_items = [(m, COLOURS["body"], 1.0) for n, m in bodies_cut.items() if n in ("body_middle_top", "body_middle_bottom", "body_back", "battery_pack_lid")]
    sec_items += [(trunk_top, COLOURS["trunk"], 1.0)] + board_items
    imgs["fit_section.png"] = render(sec_items, out / "fit_section.png", ((-68.0, 0.0, -2.0), (-68.0, -400.0, -2.0), (0, 0, 1)),
                                     parallel=36, clip=((0.0, -14.0, 0.0), (0.0, 1.0, 0.0)),
                                     title="Section at CAD y = -14 (through two bosses), looking at the robot's right side")
    usb_items = [(cut, COLOURS["body"], 1.0), (trunk_top, COLOURS["trunk"], 1.0)] + [(m, COLOURS[k], 1.0) for _, m, k in parts if k not in ("pi", "pi_parts")]
    imgs["fit_usbc.png"] = render(usb_items, out / "fit_usbc.png", ((jx, -52.0, usb_cz), (jx - 38, -150.0, usb_cz + 42), (0, 0, 1)),
                                  title="B2: body_middle_top with the 13.0 x 7.5 mm USB-C opening, the receptacle behind it")
    report["images"] = sorted(imgs)
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, out_json)
    for k in sorted(imgs):
        print("image", out / k)
    return report


if __name__ == "__main__":
    main()
