"""Copied from the quack_core research step (measure_common.py). Shared helpers: load the Open Duck Mini v2 URDF assembly (Onshape export) and
place every visual mesh in the trunk_assembly frame (zero joint angles), in mm.

Source of geometry: apirrone/Open_Duck_Mini @ b23317a, mini_bdx/robots/open_duck_mini_v2/robot.urdf
and the STL meshes beside it (copied to cad/urdf_v2/).
"""
import os, xml.etree.ElementTree as ET
import numpy as np, trimesh
from trimesh.transformations import euler_matrix, translation_matrix

# the duck CAD folder fit.py reads (DUCK_CAD): its cad/urdf_v2/ holds Open_Duck_Mini's v2 robot.urdf
# and the STL meshes beside it, as the research step copied them
URDF_DIR = os.path.join(os.environ.get('DUCK_CAD', 'duck_cad'), 'cad', 'urdf_v2')
URDF = os.path.join(URDF_DIR, 'robot.urdf')

def origin_T(el):
    if el is None:
        return np.eye(4)
    xyz = [float(v) for v in el.get('xyz', '0 0 0').split()]
    rpy = [float(v) for v in el.get('rpy', '0 0 0').split()]
    # URDF rpy = fixed-axis XYZ (roll about X, then pitch Y, then yaw Z) -> 'sxyz'
    return translation_matrix(xyz) @ euler_matrix(*rpy, axes='sxyz')

def load():
    root = ET.parse(URDF).getroot()
    joints = {}
    for j in root.findall('joint'):
        joints[j.find('child').get('link')] = (j.find('parent').get('link'), origin_T(j.find('origin')), j.get('name'))
    def link_T(name):
        T = np.eye(4)
        while name in joints:
            parent, Tj, _ = joints[name]
            T = Tj @ T
            name = parent
        return T
    parts = []  # (link, mesh_name, idx, trimesh in trunk_assembly frame, mm)
    cache = {}
    for l in root.findall('link'):
        LT = link_T(l.get('name'))
        for i, v in enumerate(l.findall('visual')):
            m = v.find('geometry/mesh')
            if m is None:
                continue
            fn = m.get('filename').split('/')[-1]
            if fn not in cache:
                cache[fn] = trimesh.load(os.path.join(URDF_DIR, fn), force='mesh')
            mesh = cache[fn].copy()
            T = LT @ origin_T(v.find('origin'))
            mesh.apply_transform(T)
            mesh.apply_scale(1000.0)  # m -> mm
            parts.append(dict(link=l.get('name'), name=fn.replace('.stl', ''), idx=i, mesh=mesh, T=T))
    return parts, link_T

def fmt_bb(b):
    return '[%7.1f %7.1f %7.1f] .. [%7.1f %7.1f %7.1f]  size %6.1f x %6.1f x %6.1f' % (*b[0], *b[1], *(b[1]-b[0]))
