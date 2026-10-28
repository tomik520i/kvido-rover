"""Local SE(2) scan matching against a PREVIOUS occupancy grid, never global relocalization.

All corrections are hypotheses within a small odometry window. A featureless wall,
weak overlap, alternate modes, or a boundary solution leaves odometry unchanged.
"""
import math

import cv2
import numpy as np

MAX_TRANSLATION_CM=10.
MAX_HEADING_DEG=8.
MAX_HITS=48


def obstacle_points(directions,reach):
    values=[]
    for ray in list(directions or [])[:96]:
        if not isinstance(ray,dict) or ray.get('status')!='obstacle':continue
        distance=ray.get('distance_cm');bearing=ray.get('bearing_deg',0.)
        if type(distance) not in (int,float) or type(bearing) not in (int,float):continue
        if not math.isfinite(distance) or not math.isfinite(bearing) or not 25<=distance<=reach:continue
        angle=math.radians(bearing)
        point=(distance*math.cos(angle),distance*math.sin(angle),bearing)
        if not any(math.hypot(point[0]-p[0],point[1]-p[1])<3 for p in values):values.append(point)
    if len(values)>MAX_HITS:
        values=[values[i] for i in np.linspace(0,len(values)-1,MAX_HITS,dtype=int)]
    return np.asarray(values,dtype=float).reshape((-1,3))


def _sample(field,x,y,cell,half,outside):
    # Bilinear field interpolation avoids choosing arbitrary cell-edge ties.
    fx=x/cell+half;fy=y/cell+half
    ix=np.floor(fx).astype(int);iy=np.floor(fy).astype(int)
    valid=(ix>=0)&(iy>=0)&(ix+1<field.shape[0])&(iy+1<field.shape[1])
    ax=np.clip(ix,0,field.shape[0]-2);ay=np.clip(iy,0,field.shape[1]-2)
    tx=fx-ix;ty=fy-iy
    result=(field[ax,ay]*(1-tx)*(1-ty)+field[ax+1,ay]*tx*(1-ty)+
            field[ax,ay+1]*(1-tx)*ty+field[ax+1,ay+1]*tx*ty)
    return np.where(valid,result,outside)


def align_scan(odds,directions,pose,reach,cell=5):
    """Return diagnostics and a bounded correction; input grid/pose are never mutated."""
    base={'status':'rejected','reason':'insufficient_obstacles',
          'correction':{'dx_cm':0.,'dy_cm':0.,'dheading_deg':0.},
          'confidence':0.,'overlap':0.,'hits':0,'position_uncertainty_cm':None,
          'heading_uncertainty_deg':None}
    points=obstacle_points(directions,reach);base['hits']=len(points)
    if len(points)<8:return base
    if np.ptp(points[:,2])<35 or np.median(np.linalg.norm(points[:,:2],axis=1))<35:
        return {**base,'reason':'limited_scan_extent'}
    eigen=np.linalg.eigvalsh(np.cov(points[:,:2],rowvar=False))
    if eigen[0]/max(eigen[1],1e-9)<.06:return {**base,'reason':'straight_wall_ambiguity'}
    occupied=np.asarray(odds)>=.6
    if occupied.sum()<8:return {**base,'reason':'insufficient_prior_map'}
    field=cv2.distanceTransform((~occupied).astype(np.uint8),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)*cell
    half=odds.shape[0]//2;x0,y0,h0=pose
    # Free samples only penalize contradictions; unknown cells never earn score.
    free=points[:,:2,None]*np.asarray([.3,.55,.8])[None,None,:]
    free=free.transpose(0,2,1).reshape((-1,2))

    def evaluate(candidates):
        angle=np.radians(h0+candidates[:,2]);co=np.cos(angle)[:,None];si=np.sin(angle)[:,None]
        px=x0+candidates[:,0,None]+co*points[:,0]-si*points[:,1]
        py=y0+candidates[:,1,None]+si*points[:,0]+co*points[:,1]
        distance=_sample(field,px,py,cell,half,100.)
        qx=x0+candidates[:,0,None]+co*free[:,0]-si*free[:,1]
        qy=y0+candidates[:,1,None]+si*free[:,0]+co*free[:,1]
        collision=_sample(occupied.astype(float),qx,qy,cell,half,0.)>.5
        scores=np.exp(-.5*(distance/3.5)**2).mean(axis=1)-.3*collision.mean(axis=1)
        overlaps=(distance<=6.5).mean(axis=1)
        return scores,overlaps,distance

    coarse=np.asarray([(x,y,a) for x in np.arange(-10,10.1,2.5)
                       for y in np.arange(-10,10.1,2.5) for a in np.arange(-8,8.1,2)])
    scores,_,_=evaluate(coarse);initial=coarse[int(np.argmax(scores))]
    fine=np.asarray([(initial[0]+x,initial[1]+y,initial[2]+a)
                     for x in (-2.5,-1.25,0,1.25,2.5) for y in (-2.5,-1.25,0,1.25,2.5)
                     for a in (-2,-1,0,1,2)
                     if abs(initial[0]+x)<=10 and abs(initial[1]+y)<=10 and abs(initial[2]+a)<=8])
    candidates=np.concatenate((coarse,fine));scores,overlaps,distances=evaluate(candidates)
    index=int(np.argmax(scores));best=candidates[index];score=float(scores[index]);overlap=float(overlaps[index])
    base.update(score=round(score,3),overlap=round(overlap,3))
    if score<.65 or overlap<.75:return {**base,'reason':'low_overlap'}
    if abs(best[0])>=9.9 or abs(best[1])>=9.9 or abs(best[2])>=7.9 or np.hypot(*best[:2])>10:
        return {**base,'reason':'correction_at_search_limit'}
    separated=(np.linalg.norm(candidates[:,:2]-best[:2],axis=1)>=7.5)|(np.abs(candidates[:,2]-best[2])>=5)
    alternate=float(np.max(scores[separated])) if separated.any() else 0.
    gap=score-alternate;base['mode_margin']=round(gap,3)
    if gap<.035:return {**base,'reason':'competing_modes_or_weak_constraint'}
    baseline=float(evaluate(np.zeros((1,3)))[0][0])
    if np.linalg.norm(best[:2])>2.5 and score-baseline<.035:return {**base,'reason':'insufficient_improvement'}
    confidence=min(1.,max(0.,overlap*score*min(1.,gap/.12)))
    if confidence<.55:return {**base,'reason':'weak_match_confidence','confidence':round(confidence,3)}
    return {**base,'status':'aligned','reason':'local_prior_map_match',
            'correction':{'dx_cm':round(float(best[0]),2),'dy_cm':round(float(best[1]),2),'dheading_deg':round(float(best[2]),2)},
            'confidence':round(confidence,3),
            'position_uncertainty_cm':round(max(3.,float(np.median(distances[index]))+2.5),2),
            'heading_uncertainty_deg':3.}
