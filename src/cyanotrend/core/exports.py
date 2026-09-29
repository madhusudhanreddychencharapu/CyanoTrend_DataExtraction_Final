"""Final notebook scene export algorithms, preserved without numerical changes.

Source: cells 50, 57, 58 and 59. Each worker has a separate Python process,
so the notebook-style context below is never shared across concurrent scenes.
"""
from __future__ import annotations
import contextlib
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import netCDF4
from scipy.spatial import cKDTree
from shapely import contains_xy
from .science import to_float
from .hydrolakes import erode_lake_geometries
from .settings import GLOBAL_ANALYSIS_RHOS_WAVELENGTHS, DEFAULT_SHORE_BUFFER_M

CYAN_CI_DETECTION_LIMIT = 1.0e-4
SCIENCE_EXPORT_DEFAULT_RESOLUTION_M = 300
SCALAR_METRICS = ["CI", "CI_cyano", "NDCI", "MPH", "FAI"] + [f"rhos_{w}" for w in GLOBAL_ANALYSIS_RHOS_WAVELENGTHS]
SCIENCE_EXPORT_VARIABLES = [f"rhos_{w}" for w in GLOBAL_ANALYSIS_RHOS_WAVELENGTHS] + ["CI", "CI_cyano", "NDCI", "MPH", "FAI"]
V267_GRID_METHOD = "epsg4326_same_lake_native_nearest_v2"
V267_EXPORT_CHUNK = 128
V267_NEAREST_RADIUS_M = 600.0
GLOBAL_STATE = {}
EXPORT_DIR = Path(".")
_COMPACT_FILES = []

def configure(lakes, files, output):
    global GLOBAL_STATE, EXPORT_DIR, _COMPACT_FILES
    GLOBAL_STATE = {"lakes": lakes}
    EXPORT_DIR = Path(output)
    _COMPACT_FILES = list(map(Path, files))

def _scene_compact_files(scene_id):
    return list(_COMPACT_FILES)

def _load_target_lakes_if_needed():
    return GLOBAL_STATE["lakes"]

def _lake_boundaries_for_ids(ids):
    try:
        lakes=_load_target_lakes_if_needed(); ids=set(map(int,ids)); return lakes[lakes.Hylak_id.astype(int).isin(ids)].copy()
    except Exception: return gpd.GeoDataFrame()


def _unit_sphere_xyz(lat,lon):
    lat=np.deg2rad(np.asarray(lat,float)); lon=np.deg2rad(np.asarray(lon,float)); c=np.cos(lat)
    return np.column_stack((c*np.cos(lon),c*np.sin(lon),np.sin(lat)))


def _load_scene_pixels(scene_id,scope='scene',lake_id=None):
    frames=[]; wanted=None if scope=='scene' else int(lake_id)
    names=[
        'latitude','longitude','Hylak_id','l2_flags',
        'valid_water_mask','cyan_strict_valid_mask','bloom_rescue_mask','cldice_mask','hisatzen_mask','navfail_mask','cloud_excluded_mask',
        'ci_valid_mask','ci_candidate_mask','ci_detection_mask','mph_valid_mask','fai_valid_mask','MPH_peak_nm',
        *SCALAR_METRICS,
    ]
    for p in _scene_compact_files(scene_id):
        with netCDF4.Dataset(p) as ds:
            ids=np.asarray(ds.variables['Hylak_id'][:],dtype=np.int64); sel=np.ones(len(ids),bool) if wanted is None else ids==wanted
            if not sel.any(): continue
            data={'Hylak_id':ids[sel]}
            for n in names:
                if n=='Hylak_id': continue
                if n=='l2_flags' and n in ds.variables:
                    data[n]=np.asarray(ds.variables[n][:],dtype=np.uint32)[sel]
                elif n in ds.variables:
                    data[n]=to_float(ds.variables[n][:])[sel]
                else:
                    data[n]=np.full(sel.sum(),np.nan)
            frames.append(pd.DataFrame(data))
    if not frames: raise ValueError('No compact lake pixels found for selected scope')
    f=pd.concat(frames,ignore_index=True)
    f['_latr']=f.latitude.round(6); f['_lonr']=f.longitude.round(6)
    f=f.drop_duplicates(['Hylak_id','_latr','_lonr']).drop(columns=['_latr','_lonr'])
    return f


def _primary_selector(frame):
    if 'valid_water_mask' not in frame.columns:
        return np.ones(len(frame),dtype=bool)
    return pd.to_numeric(frame['valid_water_mask'],errors='coerce').fillna(0).to_numpy(float)>0


def _compact_qa_metadata(scene_id):
    files=_scene_compact_files(scene_id)
    meta={"primary_qa_profile":"unknown","primary_hard_exclude_flags":[],"cyan_strict_exclude_flags":[],"conditional_cldice_bloom_recovery":False,"require_l2_nonland":False}
    if not files: return meta
    with netCDF4.Dataset(files[0]) as ds:
        meta.update({
            "primary_qa_profile":str(getattr(ds,"primary_qa_profile",getattr(ds,"schema","unknown"))),
            "primary_hard_exclude_flags":[x for x in str(getattr(ds,"primary_hard_exclude_flags","")).split(',') if x],
            "cyan_strict_exclude_flags":[x for x in str(getattr(ds,"cyan_strict_exclude_flags","")).split(',') if x],
            "conditional_cldice_bloom_recovery":bool(int(getattr(ds,"conditional_cldice_bloom_recovery",0))),
            "bloom_rescue_definition":str(getattr(ds,"bloom_rescue_definition","")),
            "bloom_rescue_method_note":str(getattr(ds,"bloom_rescue_method_note","")),
            "require_l2_nonland":bool(int(getattr(ds,"require_l2_nonland",1))),
            "shore_buffer_m":float(getattr(ds,"shore_buffer_m",0.0)),
            "ci_detection_limit":float(getattr(ds,"ci_detection_limit",CYAN_CI_DETECTION_LIMIT)),
        })
    return meta


def _science_geographic_setup_v265(frame, requested_resolution_m=SCIENCE_EXPORT_DEFAULT_RESOLUTION_M):
    """SNAP-friendly regular EPSG:4326 grid with nominal metric spacing at scene median latitude."""
    lat = pd.to_numeric(frame.latitude, errors="coerce").to_numpy(float)
    lon = pd.to_numeric(frame.longitude, errors="coerce").to_numpy(float)
    finite = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
    if not finite.any():
        raise ValueError("No finite stored scene geolocation")
    res = float(requested_resolution_m)
    ref_lat = float(np.nanmedian(lat[finite]))
    dlat = res / 111320.0
    dlon = res / max(111320.0 * math.cos(math.radians(ref_lat)), 1000.0)
    west = math.floor(float(np.nanmin(lon[finite])) / dlon) * dlon - dlon
    east = math.ceil(float(np.nanmax(lon[finite])) / dlon) * dlon + dlon
    south = math.floor(float(np.nanmin(lat[finite])) / dlat) * dlat - dlat
    north = math.ceil(float(np.nanmax(lat[finite])) / dlat) * dlat + dlat
    nx = max(2, int(round((east - west) / dlon)))
    ny = max(2, int(round((north - south) / dlat)))
    east = west + nx * dlon; north = south + ny * dlat
    longitude = west + (np.arange(nx, dtype=np.float64) + 0.5) * dlon
    latitude = south + (np.arange(ny, dtype=np.float64) + 0.5) * dlat
    return {
        "resolution_m": res, "reference_latitude": ref_lat, "dlat": dlat, "dlon": dlon,
        "west": west, "east": east, "south": south, "north": north,
        "nx": nx, "ny": ny, "ncell": int(nx)*int(ny),
        "longitude": longitude, "latitude": latitude, "crs": "EPSG:4326",
    }


def _scene_analysis_lakes_v267(scene_id, frame, clip_geom=None):
    """Return the exact scene lake polygons used as mapped support.

    The shoreline erosion stored in the compact-product metadata is reapplied so the
    mapped product does not expand back into shoreline pixels intentionally removed
    during native-pixel extraction.
    """
    ids = pd.to_numeric(frame.get("Hylak_id", pd.Series(dtype=float)), errors="coerce").dropna().astype(np.int64).unique()
    if not len(ids):
        raise ValueError("No Hylak_id values are available in the selected scene/scope")
    lakes = _lake_boundaries_for_ids(ids)
    if lakes is None or lakes.empty:
        raise RuntimeError("HydroLAKES polygons for the stored scene lakes are unavailable; reload the target HydroLAKES universe before mapping/export.")
    lakes = lakes.to_crs(4326).copy()
    qa = _compact_qa_metadata(scene_id)
    shore = float(qa.get("shore_buffer_m", DEFAULT_SHORE_BUFFER_M) or 0.0)
    if shore > 0:
        lakes = erode_lake_geometries(lakes, shore)
    if clip_geom is not None and not getattr(clip_geom, "is_empty", True):
        cg = clip_geom
        try:
            # Planning geometries are stored in EPSG:4326 in this app.
            lakes["geometry"] = lakes.geometry.intersection(cg)
            lakes = lakes[~lakes.geometry.is_empty].copy()
        except Exception:
            pass
    if lakes.empty:
        raise ValueError("No mapped lake polygons remain in the selected display/export scope")
    return lakes


def _dedupe_target_assignments_v267(rows, cols, src, dist, nx):
    """Keep one source per target cell, preferring a real match and then shortest distance."""
    rows = np.asarray(rows, np.int64); cols = np.asarray(cols, np.int64)
    src = np.asarray(src, np.int64); dist = np.asarray(dist, float)
    if not len(rows):
        return rows, cols, src, dist
    flat = rows * int(nx) + cols
    # Unmatched support cells sort behind matched cells. Among matches choose nearest.
    rank = np.where(src >= 0, dist, np.inf)
    order = np.lexsort((rank, flat))
    flat_o = flat[order]
    keep = np.r_[True, flat_o[1:] != flat_o[:-1]]
    take = order[keep]
    return rows[take], cols[take], src[take], dist[take]


def _native_assignments_geographic_v267(frame, g, scene_id, clip_geom=None, radius_m=None):
    """Generate target lake cells and same-lake nearest-native source assignments.

    A target cell is never allowed to borrow an observation from a different Hylak_id.
    The nearest source is chosen before QA is inspected. Thus a cloud/invalid native
    observation remains no-data rather than being replaced by a farther clear pixel.
    """
    radius_m = max(float(radius_m or V267_NEAREST_RADIUS_M), 2.0 * float(g["resolution_m"]))
    lat = pd.to_numeric(frame.latitude, errors="coerce").to_numpy(float)
    lon = pd.to_numeric(frame.longitude, errors="coerce").to_numpy(float)
    hid = pd.to_numeric(frame.Hylak_id, errors="coerce").fillna(-1).to_numpy(np.int64)
    finite = np.isfinite(lat) & np.isfinite(lon)
    lakes = _scene_analysis_lakes_v267(scene_id, frame, clip_geom=clip_geom)

    all_r=[]; all_c=[]; all_s=[]; all_d=[]
    lats_grid=np.asarray(g["latitude"],float); lons_grid=np.asarray(g["longitude"],float)
    for _, lr in lakes.iterrows():
        lid=int(lr.Hylak_id); geom=lr.geometry
        src_idx=np.flatnonzero(finite & (hid==lid))
        if not len(src_idx) or geom is None or geom.is_empty:
            continue
        tree=cKDTree(_unit_sphere_xyz(lat[src_idx],lon[src_idx]))
        west,south,east,north=map(float,geom.bounds)
        c0=max(0,int(np.searchsorted(lons_grid,west,side="left")-1)); c1=min(g["nx"],int(np.searchsorted(lons_grid,east,side="right")+1))
        r0=max(0,int(np.searchsorted(lats_grid,south,side="left")-1)); r1=min(g["ny"],int(np.searchsorted(lats_grid,north,side="right")+1))
        if c1<=c0 or r1<=r0: continue
        width=max(1,c1-c0); block_rows=max(1,min(r1-r0,int(max(1,350000//width))))
        for rb0 in range(r0,r1,block_rows):
            rb1=min(rb0+block_rows,r1)
            lon2,lat2=np.meshgrid(lons_grid[c0:c1],lats_grid[rb0:rb1])
            support=contains_xy(geom,lon2,lat2)
            pos=np.flatnonzero(support.ravel())
            if not len(pos): continue
            rr_local,cc_local=np.unravel_index(pos,support.shape)
            rr=rr_local.astype(np.int64)+rb0; cc=cc_local.astype(np.int64)+c0
            xyz=_unit_sphere_xyz(lat2.ravel()[pos],lon2.ravel()[pos])
            chord,ii=tree.query(xyz,k=1,workers=-1)
            dist=6371000.0*(2.0*np.arcsin(np.minimum(1.0,np.asarray(chord,float)/2.0)))
            matched=np.isfinite(dist)&(dist<=radius_m)&(np.asarray(ii)<len(src_idx))
            src=np.full(len(pos),-1,np.int64); dd=np.full(len(pos),np.nan,float)
            if matched.any():
                src[matched]=src_idx[np.asarray(ii[matched],np.int64)]
                dd[matched]=dist[matched]
            all_r.append(rr); all_c.append(cc); all_s.append(src); all_d.append(dd)
    if not all_r:
        return (np.empty(0,np.int64),)*3 + (np.empty(0,float),)
    rows=np.concatenate(all_r); cols=np.concatenate(all_c); src=np.concatenate(all_s); dist=np.concatenate(all_d)
    return _dedupe_target_assignments_v267(rows,cols,src,dist,g["nx"])


def export_complete_scene_science_netcdf(scene_id, requested_resolution_m=SCIENCE_EXPORT_DEFAULT_RESOLUTION_M, force=False):
    """Production share raster: exact nominal grid, same-lake nearest-native remap, sparse writes."""
    if not scene_id: raise ValueError("Choose a completed scene")
    frame=_load_scene_pixels(scene_id,"scene",None).copy();g=_science_geographic_setup_v265(frame,float(requested_resolution_m));export_dir=EXPORT_DIR/"snap_complete";export_dir.mkdir(parents=True,exist_ok=True);out=export_dir/f"S3_OLCI_{scene_id}_ALL_SCIENCE_{int(round(g['resolution_m']))}m_CF.nc";source_files=_scene_compact_files(scene_id);newest=max((p.stat().st_mtime_ns for p in source_files),default=0)
    if out.exists() and not force and out.stat().st_mtime_ns>=newest:
        with contextlib.suppress(Exception):
            with netCDF4.Dataset(out) as old:
                if getattr(old,"science_grid_method","")==V267_GRID_METHOD and float(getattr(old,"nominal_grid_resolution_m",-1))==float(g["resolution_m"]): return str(out),g
    rows,cols,src,dist=_native_assignments_geographic_v267(frame,g,scene_id);matched=src>=0;primary=_primary_selector(frame);qa=_compact_qa_metadata(scene_id);source_values={n:pd.to_numeric(frame[n],errors="coerce").to_numpy(float) for n in SCIENCE_EXPORT_VARIABLES if n in frame.columns};source_hylak=pd.to_numeric(frame.Hylak_id,errors="coerce").fillna(-1).to_numpy(np.int32)
    mask_input_names=["valid_water_mask","cyan_strict_valid_mask","bloom_rescue_mask","cldice_mask","hisatzen_mask","navfail_mask","cloud_excluded_mask","ci_valid_mask","ci_candidate_mask","ci_detection_mask","mph_valid_mask","fai_valid_mask"]
    source_masks={n:pd.to_numeric(frame[n],errors="coerce").fillna(0).to_numpy(float)>0 for n in mask_input_names if n in frame.columns}
    mask_output_name={"cyan_strict_valid_mask":"generic_flag_exclusion_sensitivity_mask"}
    tmp=out.with_suffix(".nc.tmp");tmp.unlink(missing_ok=True);chunk=int(V267_EXPORT_CHUNK)
    with netCDF4.Dataset(tmp,"w",format="NETCDF4_CLASSIC") as ds:
        ds.createDimension("latitude",g["ny"]);ds.createDimension("longitude",g["nx"]);ds.setncatts({"Conventions":"CF-1.8","title":"Sentinel-3 OLCI HydroLAKES bloom-aware all-science efficient 300 m product","scene_id":str(scene_id),"science_grid_method":V267_GRID_METHOD,"nominal_grid_resolution_m":float(g["resolution_m"]),"actual_grid_resolution_m":float(g["resolution_m"]),"cell_height_m_nominal":float(g["resolution_m"]),"cell_width_m_at_reference_latitude":float(g["resolution_m"]),"reference_latitude_for_longitude_spacing":float(g["reference_latitude"]),"geospatial_lat_min":float(g["south"]),"geospatial_lat_max":float(g["north"]),"geospatial_lon_min":float(g["west"]),"geospatial_lon_max":float(g["east"]),"latitude_step_degrees":float(g["dlat"]),"longitude_step_degrees":float(g["dlon"]),"nearest_native_radius_m":max(V267_NEAREST_RADIUS_M,2.0*float(g["resolution_m"])),"mapping":"each target lake cell maps only to the nearest stored native OLCI observation from the same Hylak_id","qa_rule":"nearest same-lake source is chosen before QA; if that native source is not primary-valid, science value remains fill/NaN","resampling":"nearest-native assignment; no averaging/value interpolation or gap filling","striping_policy":"target lake support is populated from same-lake nearest native observations instead of center-only binning","primary_qa_mask":"valid_water_mask (bloom-aware)","generic_CLDICE_sensitivity":"diagnostic only; not claimed equivalent to NASA CyAN upstream cloud processing","NASA_area_weighting":"not used in production v2.6.7; retained as optional validation concept only","statistics_source":"native compact OLCI lake observations, not mapped raster","true_color_mapping":"red=rhos_665; green=rhos_560; blue=rhos_490","CI_cyano_detection_limit":float(CYAN_CI_DETECTION_LIMIT),"qa_policy_json":json.dumps(qa,default=str)})
        yv=ds.createVariable("latitude","f8",("latitude",));yv[:]=g["latitude"];yv.setncatts({"standard_name":"latitude","units":"degrees_north","axis":"Y"});xv=ds.createVariable("longitude","f8",("longitude",));xv[:]=g["longitude"];xv.setncatts({"standard_name":"longitude","units":"degrees_east","axis":"X"});from pyproj import CRS;wkt4326=CRS.from_epsg(4326).to_wkt();crsv=ds.createVariable("crs","i4");crsv.assignValue(0);crsv.setncatts({"grid_mapping_name":"latitude_longitude","epsg_code":"EPSG:4326","spatial_ref":wkt4326,"crs_wkt":wkt4326,"semi_major_axis":6378137.0,"inverse_flattening":298.257223563})
        chunks=(min(chunk,g["ny"]),min(chunk,g["nx"]));vars_float={};long_names={"CI":"Cyanobacteria Index candidate magnitude","CI_cyano":"cyanobacteria index detections","NDCI":"Normalized Difference Chlorophyll Index","MPH":"Maximum Peak Height","FAI":"OLCI-adapted Floating Algae Index (AFAI form)"}
        for sn,on in [(n,"rhos_884" if n=="rhos_885" else n) for n in SCIENCE_EXPORT_VARIABLES if n in source_values]:
            v=ds.createVariable(on,"f4",("latitude","longitude"),zlib=True,complevel=6,shuffle=True,chunksizes=chunks,fill_value=np.float32(-9999));v.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","units":"1","long_name":long_names.get(on,f"Rayleigh-corrected OLCI reflectance {on}"),"qa_basis":"primary bloom-aware valid_water_mask"});vars_float[sn]=v
        vars_mask={}
        for sn in source_masks:
            on=mask_output_name.get(sn,sn);v=ds.createVariable(on,"i1",("latitude","longitude"),zlib=True,complevel=6,shuffle=True,chunksizes=chunks,fill_value=np.int8(-1));v.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","long_name":on.replace("_"," "),"flag_values":np.array([0,1],dtype=np.int8),"flag_meanings":"false true","valid_range":np.array([0,1],dtype=np.int8)});vars_mask[sn]=v
        support_var=ds.createVariable("lake_support_mask","i1",("latitude","longitude"),zlib=True,complevel=5,shuffle=True,chunksizes=chunks,fill_value=np.int8(-1));support_var.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","long_name":"target cell lies inside the analysis HydroLAKES polygon after shoreline buffer","flag_values":np.array([0,1],dtype=np.int8),"flag_meanings":"false true"})
        match_var=ds.createVariable("native_source_match_mask","i1",("latitude","longitude"),zlib=True,complevel=5,shuffle=True,chunksizes=chunks,fill_value=np.int8(-1));match_var.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","long_name":"target lake cell has a same-lake native OLCI source within search radius","flag_values":np.array([0,1],dtype=np.int8),"flag_meanings":"false true"})
        dist_var=ds.createVariable("native_match_distance_m","f4",("latitude","longitude"),zlib=True,complevel=5,shuffle=True,chunksizes=chunks,fill_value=np.float32(-9999));dist_var.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","units":"m","long_name":"distance to nearest same-lake stored native OLCI observation"});hylak_var=ds.createVariable("nearest_Hylak_id","i4",("latitude","longitude"),zlib=True,complevel=5,shuffle=True,chunksizes=chunks,fill_value=np.int32(-1));hylak_var.setncatts({"coordinates":"latitude longitude","grid_mapping":"crs","long_name":"HydroLAKES identifier of same-lake native source"})
        if len(rows):
            nxc=int(math.ceil(g["nx"]/chunk));cid=(rows//chunk)*nxc+(cols//chunk);order=np.argsort(cid,kind="mergesort");cids=cid[order];cuts=np.flatnonzero(np.r_[True,cids[1:]!=cids[:-1],True])
            for a,b in zip(cuts[:-1],cuts[1:]):
                pos=order[a:b];r0=int((rows[pos[0]]//chunk)*chunk);c0=int((cols[pos[0]]//chunk)*chunk);r1=min(r0+chunk,g["ny"]);c1=min(c0+chunk,g["nx"]);rr=rows[pos]-r0;cc=cols[pos]-c0;spos=src[pos];m=spos>=0
                sb=np.full((r1-r0,c1-c0),np.int8(-1),np.int8);sb[rr,cc]=1;support_var[r0:r1,c0:c1]=np.ma.masked_equal(sb,np.int8(-1));mb=np.full_like(sb,np.int8(-1));mb[rr,cc]=m.astype(np.int8);match_var[r0:r1,c0:c1]=np.ma.masked_equal(mb,np.int8(-1));db=np.full((r1-r0,c1-c0),np.float32(-9999),np.float32);valid_d=m&np.isfinite(dist[pos]);db[rr[valid_d],cc[valid_d]]=dist[pos][valid_d].astype(np.float32);dist_var[r0:r1,c0:c1]=np.ma.masked_equal(db,np.float32(-9999));hb=np.full((r1-r0,c1-c0),np.int32(-1),np.int32);hb[rr[m],cc[m]]=source_hylak[spos[m]];hylak_var[r0:r1,c0:c1]=np.ma.masked_equal(hb,np.int32(-1))
                for sn,v in vars_float.items():
                    block=np.full((r1-r0,c1-c0),np.float32(-9999),np.float32)
                    if m.any():
                        vv=source_values[sn][spos[m]];ok=np.isfinite(vv)&primary[spos[m]];block[rr[m][ok],cc[m][ok]]=vv[ok].astype(np.float32)
                    v[r0:r1,c0:c1]=np.ma.masked_equal(block,np.float32(-9999))
                for sn,v in vars_mask.items():
                    block=np.full((r1-r0,c1-c0),np.int8(-1),np.int8)
                    if m.any(): block[rr[m],cc[m]]=source_masks[sn][spos[m]].astype(np.int8)
                    v[r0:r1,c0:c1]=np.ma.masked_equal(block,np.int8(-1))
    tmp.replace(out);g.update({"science_grid_method":V267_GRID_METHOD,"target_lake_cells":int(len(rows)),"matched_target_cells":int(matched.sum()),"nearest_radius_m":max(V267_NEAREST_RADIUS_M,2.0*float(g["resolution_m"]))});return str(out),g


def _share_all_index_stats(scene_id,cfg,row):
    """Lean native-observation statistics; generic CLDICE exclusion is counts-only sensitivity QA."""
    frame=_load_scene_pixels(scene_id,"scene",None).copy()
    if frame.empty: raise ValueError("No compact lake pixels found for selected scene")
    for c in ("latitude","longitude","Hylak_id"): frame[c]=pd.to_numeric(frame[c],errors="coerce")
    frame=frame.dropna(subset=["latitude","longitude","Hylak_id"]).copy();frame["_lat6"]=frame.latitude.round(6);frame["_lon6"]=frame.longitude.round(6);frame=frame.drop_duplicates(["Hylak_id","_lat6","_lon6"]).drop(columns=["_lat6","_lon6"])
    lakes=GLOBAL_STATE.get("lakes");lake_meta={}
    if lakes is not None and not lakes.empty:
        for _,lr in lakes[lakes.Hylak_id.astype(int).isin(frame.Hylak_id.astype(int).unique())].iterrows():lake_meta[int(lr.Hylak_id)]={"Lake_name":lr.get("Lake_name","") or "","Lake_area_km2":float(lr.get("Lake_area",np.nan))}
    def mask(g,n):return pd.to_numeric(g.get(n,pd.Series(np.zeros(len(g)),index=g.index)),errors="coerce").fillna(0).to_numpy(float)>0
    def stat(s):
        a=pd.to_numeric(s,errors="coerce").to_numpy(float);a=a[np.isfinite(a)]
        if not len(a):return {"valid_pixels":0,"mean":np.nan,"median":np.nan,"p10":np.nan,"p90":np.nan,"max":np.nan}
        return {"valid_pixels":int(len(a)),"mean":float(np.mean(a)),"median":float(np.median(a)),"p10":float(np.percentile(a,10)),"p90":float(np.percentile(a,90)),"max":float(np.max(a))}
    recs=[]
    for lid,gdf in frame.groupby(frame.Hylak_id.astype(int),sort=True):
        primary=mask(gdf,"valid_water_mask");sens=mask(gdf,"cyan_strict_valid_mask");rescue=mask(gdf,"bloom_rescue_mask");cld=mask(gdf,"cldice_mask");cloud_ex=mask(gdf,"cloud_excluded_mask");cand=mask(gdf,"ci_candidate_mask");det=mask(gdf,"ci_detection_mask");ci_valid=mask(gdf,"ci_valid_mask")
        rec={"scene_id":str(scene_id),"scene_name":str(row.scene_name),"acquisition_start":str(row.acquisition_start),"Hylak_id":int(lid),**lake_meta.get(int(lid),{}),"native_lake_pixels":int(len(gdf)),"primary_retained_pixels":int(primary.sum()),"generic_flag_exclusion_sensitivity_pixels":int(sens.sum()),"bloom_rescued_pixels":int(rescue.sum()),"CLDICE_pixels":int(cld.sum()),"cloud_excluded_pixels":int(cloud_ex.sum()),"bloom_rescue_fraction_of_CLDICE":float(rescue.sum()/cld.sum()) if cld.sum() else np.nan,"ci_input_valid_pixels":int(ci_valid.sum()),"ci_candidate_pixels":int(cand.sum()),"ci_cyano_detection_pixels":int(det.sum())}
        bases={"CI":cand,"CI_cyano":det,"NDCI":primary,"MPH":primary,"FAI":primary}
        for metric,basis in bases.items():
            s=stat(gdf.loc[basis,metric] if metric in gdf else pd.Series(dtype=float))
            for k,v in s.items():rec[f"{metric}_{k}"]=v
        recs.append(rec)
    table=pd.DataFrame(recs).sort_values("Hylak_id").reset_index(drop=True);d=EXPORT_DIR/"scene_bundles";d.mkdir(parents=True,exist_ok=True);p=d/f"S3_OLCI_{scene_id}_ALL_INDEX_STATS.csv";table.to_csv(p,index=False);return p,table

from typing import Any
import re
import zipfile
from .utils import utc_now
S3_FRAME_RE = re.compile(r"^(S3[AB])_OL_1_EFR____(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{4})_(\d{3})_(\d{3})_(\d{4})_")
_REGISTRY = None

def registry_table(config_hash):
    return _REGISTRY

def _app_status(title, text):
    return title + ": " + text

def parse_s3_frame_metadata(name: str) -> dict[str, Any]:
    n = Path(str(name)).name.removesuffix(".SEN3").removesuffix(".zip")
    m = S3_FRAME_RE.match(n)
    if not m:
        raise ValueError(f"Cannot parse Sentinel-3 OLCI frame fields from {n}")
    platform, start, stop, created, duration, cycle, relative_orbit, frame = m.groups()
    return {
        "scene_name": n, "platform": platform, "sensing_start": start,
        "sensing_stop": stop, "creation_time": created, "duration_s": int(duration),
        "cycle": int(cycle), "relative_orbit": int(relative_orbit), "frame": int(frame),
        "native_frame_id": f"{platform}_R{int(relative_orbit):03d}_F{int(frame):04d}",
    }

def create_scene_share_bundle(scene_id,science_resolution_m=SCIENCE_EXPORT_DEFAULT_RESOLUTION_M):
    """Exactly three files from the efficient production branch."""
    if not scene_id: raise ValueError("Choose a completed scene")
    t=registry_table(GLOBAL_STATE.get("config_hash"));r=t[t.scene_id.astype(str)==str(scene_id)]
    if r.empty: raise KeyError(scene_id)
    row=r.iloc[0];cfg=str(row.config_hash);d=EXPORT_DIR/"scene_bundles";d.mkdir(parents=True,exist_ok=True);complete_nc,g=export_complete_scene_science_netcdf(scene_id,float(science_resolution_m),force=False);stats_csv,stats_table=_share_all_index_stats(scene_id,cfg,row);qa=_compact_qa_metadata(scene_id);frame_meta=parse_s3_frame_metadata(row.scene_name)
    meta={"schema":"global-efficient-share-v2.6.7","created_utc":utc_now(),"scene_id":str(scene_id),"scene_name":str(row.scene_name),"acquisition_start":str(row.acquisition_start),"config_hash":cfg,**frame_meta,"files":{"snap_all_science_netcdf":Path(complete_nc).name,"all_index_stats_csv":Path(stats_csv).name},"snap_variables":["rhos_490","rhos_560","rhos_620","rhos_665","rhos_681","rhos_709","rhos_754","rhos_865","rhos_884","CI","CI_cyano","NDCI","MPH","FAI","valid_water_mask","generic_flag_exclusion_sensitivity_mask","bloom_rescue_mask","cldice_mask","hisatzen_mask","lake_support_mask","native_source_match_mask","native_match_distance_m","nearest_Hylak_id"],"snap_grid":{"projection":"EPSG:4326 geographic","grid_method":V267_GRID_METHOD,"nominal_resolution_m":float(g["resolution_m"]),"reference_latitude":float(g["reference_latitude"]),"latitude_step_degrees":float(g["dlat"]),"longitude_step_degrees":float(g["dlon"]),"nearest_radius_m":float(g.get("nearest_radius_m",max(V267_NEAREST_RADIUS_M,2.0*float(g["resolution_m"])))),"mapping":"same-lake nearest native observation","interpolation":"none","gap_fill":"none","area_weighting":"not used in production"},"masking":qa,"strict_mask_policy":"Generic LAND+CLDICE+HISATZEN-style exclusion is retained only as a sensitivity mask/count; it is not called CyAN-equivalent because NASA upstream cloud processing differs.","statistics":{"source":"native compact OLCI observations","mapped_raster_not_used_for_statistics":True},"future_daily_composite":"S3A/S3B daily CIcyano maximum is intentionally not implemented yet; add only after this efficient mapping branch is validated.","important":"The mapped raster is for visualization/GIS/SNAP interoperability. Quantitative lake statistics remain native-observation based."}
    mp=d/f"S3_OLCI_{scene_id}_METADATA.json";mp.write_text(json.dumps(meta,indent=2,default=str));z=d/f"{str(row.scene_name)}_{cfg}_SHARE_MINIMAL.zip"
    with zipfile.ZipFile(z,"w",zipfile.ZIP_DEFLATED,allowZip64=True) as arc:
        for p in (Path(complete_nc),Path(stats_csv),mp):arc.write(p,arcname=p.name)
    return str(z),_app_status("Minimal share bundle created",f"3 files · nominal {g['resolution_m']:.0f} m EPSG:4326 · same-lake native-nearest · bloom-aware QA · no area-weighting overhead")
