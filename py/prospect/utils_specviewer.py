# -*- coding: utf-8 -*-

"""
Utility functions for prospect
"""

import numpy as np
import astropy.io.fits
from astropy.table import Table, vstack
import scipy.ndimage.filters


import matplotlib
matplotlib.use('Agg') # No DISPLAY
import matplotlib.pyplot as plt

from desiutil.log import get_logger
import desispec.spectra
import desispec.frame
from desitarget.targetmask import desi_mask
from desitarget.cmx.cmx_targetmask import cmx_mask
from desitarget.sv1.sv1_targetmask import desi_mask as sv1_desi_mask
from prospect import mycoaddcam
from prospect import myspecselect

_vi_flags = [
    # Definition of VI flags
    # Replaces former list viflags = ["Yes","No","Maybe","LowSNR","Bad"]
    # shortlabels for "issue" flags must be a unique single-letter identifier
    {"label" : "4", "type" : "class", "description" : "Confident classification, two or more secure features"},
    {"label" : "3", "type" : "class", "description" : "Probable classification, at least one secure feature + continuum; or many weak features"},
    {"label" : "2", "type" : "class", "description" : "Possible classification, one strong emission feature, but not sure what it is"},
    {"label" : "1", "type" : "class", "description" : "Unlikely classification, one or some unidentified features"},
    {"label" : "0", "type" : "class", "description" : "Nothing there"},
    {"label" : "Bad redshift fit", "shortlabel" : "R", "type" : "issue", "description" : "Misestimation of redshift by pipeline fitter"},
    {"label" : "Bad spectype fit", "shortlabel" : "C", "type" : "issue", "description" : "Misidentification of spectral category by pipeline fitter, eg. star vs QSO..."},
    {"label" : "Bad spectrum", "shortlabel" : "S", "type" : "issue", "description" : "Bad spectrum, eg. cosmic / skyline subtraction residuals..."}
]

_vi_file_fields = [
    # Contents of VI files: [ 
    #      field name (in VI file header), 
    #      associated variable in cds_targetinfo, 
    #      dtype in VI file ]
    # Ordered list
    ["TargetID", "targetid", "i4"],
    ["ExpID", "expid", "i4"],
    ["Spec version", "spec_version", "i4"], # TODO define
    ["Redrock version", "redrock_version", "i4"], # TODO define
    ["Redrock spectype", "spectype", "S10"],
    ["Redrock z", "z", "f4"],
    ["VI scanner", "VI_scanner", "S10"],
    ["VI class", "VI_class_flag", "i2"],
    ["VI issue", "VI_issue_flag", "S6"],
    ["VI z", "VI_z", "f4"],
    ["VI spectype", "VI_spectype", "S10"],
    ["VI comment", "VI_comment", "S100"]
]

_vi_spectypes =[
    # List of spectral types to fill in VI categories
    # in principle, it should match somehow redrock spectypes...
    "STAR",
    "GALAXY",
    "QSO"
]

def read_vi(vifile) :
    '''
    Read visual inspection file (ASCII/CSV or FITS according to file extension)
    Return full VI catalog, in Table format
    '''
    vi_records = [x[0] for x in _vi_file_fields]
    vi_dtypes = [x[2] for x in _vi_file_fields]
    
    if (vifile[-5:] != ".fits" and vifile[-4:] not in [".fit",".fts",".csv"]) :
        raise RuntimeError("wrong file extension")
    if vifile[-4:] == ".csv" :
        vi_info = Table.read(vifile,format='ascii.csv', names=vi_records)
        for i,rec in enumerate(vi_records) :
            vi_info[rec] = vi_info[rec].astype(vi_dtypes[i])
    else :
        vi_info = astropy.io.fits.getdata(vifile,1)
        if [(x in vi_info.names) for x in vi_records]!=[1 for x in vi_records] :
            raise RuntimeError("wrong record names in VI fits file")
        vi_info = Table(vi_info)

    return vi_info


def match_vi_targets(vifile, targetlist) :
    '''
    Returns list of VIs matching the list of targetids
    For a given target, several VI entries can be available
    '''
    vi_info = read_vi(vifile)
    vicatalog=[ [] for i in range(len(targetlist)) ]
    for itarget,targetnum in enumerate(targetlist) :
        w,=np.where( (vi_info['targetid'] == targetnum) )
        if len(w)>0 : vicatalog[itarget] = vi_info[w]
    return vicatalog


def convert_vi_tofits(vifile_in, overwrite=True) :
    log = get_logger()
    if vifile_in[-4:] != ".csv" : raise RuntimeError("wrong file extension")
    vi_info = read_vi(vifile_in)
    vifile_out=vifile_in.replace(".csv",".fits")
    vi_info.write(vifile_out, format='fits', overwrite=overwrite)
    log.info("Created fits file : "+vifile_out+" ("+str(len(vi_info))+" entries).")
    

def initialize_master_vi(mastervifile, overwrite=False) :
    '''
    Create "master" VI file with no entry
    '''
    log = get_logger()
    vi_records = [x[0] for x in _vi_file_fields]
    vi_dtypes = [x[2] for x in _vi_file_fields]
    vi_info = Table(names=vi_records, dtype=tuple(vi_dtypes))
    vi_info.write(mastervifile, format='fits', overwrite=overwrite)
    log.info("Initialized VI file : "+mastervifile+" (0 entry)")
    

def merge_vi(mastervifile, newvifile) :
    '''
    Merge a new VI file to the "master" VI file
    The master file is overwritten.
    '''
    log = get_logger()
    mastervi = read_vi(mastervifile)
    newvi = read_vi(newvifile)
    mergedvi = vstack([mastervi,newvi], join_type='exact')
    mergedvi.write(mastervifile, format='fits', overwrite=True)
    log.info("Updated master VI file : "+mastervifile+" (now "+str(len(mergedvi))+" entries).")


def match_zcat_to_spectra(zcat_in, spectra) :
    '''
    zcat_in : astropy Table from redshift fitter
    creates a new astropy Table whose rows match the targetids of input spectra
    also returns the corresponding list of indices
    '''
    zcat_out = Table(dtype=zcat_in.dtype)
    index_list = list()
    for i_spec in range(spectra.num_spectra()) :
        ww, = np.where((zcat_in['TARGETID'] == spectra.fibermap['TARGETID'][i_spec]))
        if len(ww)<1 : raise RuntimeError("zcat table cannot match spectra.")
        zcat_out.add_row(zcat_in[ww[0]])
        index_list.append(ww[0])
    return (zcat_out, index_list)


def get_y_minmax(pmin, pmax, data, ispec) :
    '''
    Utility, from plotframe
    '''
    dx = np.sort(data[np.isfinite(data)])
    if len(dx)==0 : return (0,0)
    imin = int(np.floor(pmin*len(dx)))
    imax = int(np.floor(pmax*len(dx)))
    if (imax >= len(dx)) : imax = len(dx)-1
    return (dx[imin],dx[imax])


def miniplot_spectrum(spectra, i_spec, model=None, saveplot=None, smoothing=-1, coaddcam=True) :
    '''
    Matplotlib version of plotspectra, to plot a given spectrum
    Pieces of code were copy-pasted from plotspectra()
    Smoothing option : simple gaussian filtering
    '''
    data=[]
    if coaddcam is True :
        wave, flux, ivar = mycoaddcam.mycoaddcam(spectra)
        bad = ( ivar == 0.0 )
        flux[bad] = np.nan
        thedat = dict(
                band = 'coadd',
                wave = wave,
                flux = flux[i_spec]
                )
        data.append(thedat)
    else :
        for band in spectra.bands :
            #- Set masked bins to NaN so that Bokeh won't plot them
            bad = (spectra.ivar[band] == 0.0) | (spectra.mask[band] != 0)
            spectra.flux[band][bad] = np.nan
            thedat=dict(
                band = band,
                wave = spectra.wave[band].copy(),
                flux = spectra.flux[band][i_spec].copy()
                )
            data.append(thedat)
    
    if model is not None:
        mwave, mflux = model
        mwave = mwave.copy() # in case of
        mflux = mflux[i_spec].copy()

    # Gaussian smoothing
    if smoothing > 0 :
        ymin,ymax=0,0
        for spec in data :
            spec['wave'] = spec['wave']
            spec['flux'] = scipy.ndimage.filters.gaussian_filter1d(spec['flux'], sigma=smoothing, mode='nearest')
            tmpmin,tmpmax=get_y_minmax(0.01, 0.99, spec['flux'],i_spec)
            ymin=np.nanmin((tmpmin,ymin))
            ymax=np.nanmax((tmpmax,ymax))
        if model is not None :
            mwave = mwave[int(smoothing):-int(smoothing)]
            mflux = scipy.ndimage.filters.gaussian_filter1d(mflux, sigma=smoothing, mode='nearest')[int(smoothing):-int(smoothing)]
    
    colors = dict(b='#1f77b4', r='#d62728', z='maroon', coadd='#d62728')
    # for visibility, do not plot near-edge of band data (noise is high there):
    waverange = dict(b=[3500,5800], r=[5800,7600], z=[7600,9900], coadd=[3500,9900])
    for spec in data :
        band = spec['band']
        w, = np.where( (spec['wave']>=waverange[band][0]) & (spec['wave']<=waverange[band][1]) )
        plt.plot(spec['wave'][w],spec['flux'][w],c=colors[band])
    if model is not None :
        plt.plot(mwave, mflux, c='k')
    # No label to save space
    if smoothing > 0 :
        ymin = ymin*1.4 if (ymin<0) else ymin*0.6
        ymax = ymax*1.4
        plt.ylim((ymin,ymax))
    # TODO : include some infos on plot
    
    if saveplot is not None : plt.savefig(saveplot, dpi=50) # default dpi=100, TODO tune dpi
    else : print("No plot saved?") # TODO saveplot kwd optional or not ?
    plt.clf()
        
    return




def frames2spectra(frames, nspec=None, startspec=None, with_scores=False, with_resolution_data=False):
    '''Convert input list of Frames into Spectra object
    with_score : if true, propagate scores
    with_resolution_data: if true, propagate resolution
    '''
    bands = list()
    wave = dict()
    flux = dict()
    ivar = dict()
    mask = dict()
    res = dict()
        
    for fr in frames:
        fibermap = fr.fibermap
        band = fr.meta['CAMERA'][0]
        bands.append(band)
        wave[band] = fr.wave
        flux[band] = fr.flux
        ivar[band] = fr.ivar
        mask[band] = fr.mask
        res[band] = fr.resolution_data
        if nspec is not None :
            if startspec is None : startspec = 0
            flux[band] = flux[band][startspec:nspec+startspec]
            ivar[band] = ivar[band][startspec:nspec+startspec]
            mask[band] = mask[band][startspec:nspec+startspec]
            res[band] = res[band][startspec:nspec+startspec,:,:]
            fibermap = fr.fibermap[startspec:nspec+startspec]
    
    merged_scores = None
    if with_scores :
        scores_columns = frames[0].scores.columns
        for i in range(1,len(frames)) : 
            scores_columns += frames[i].scores.columns
        merged_scores = astropy.io.fits.FITS_rec.from_columns(scores_columns)

    if not with_resolution_data : res = None
    
    spectra = desispec.spectra.Spectra(
        bands, wave, flux, ivar, mask, fibermap=fibermap, meta=fr.meta, scores=merged_scores, resolution_data=res
    )
    return spectra


def specviewer_selection(spectra, log=None, mask=None, mask_type=None, gmag_cut=None, rmag_cut=None, chi2cut=None, zbest=None, snr_cut=None) :
    '''
    Simple sub-selection on spectra based on meta-data.
        Implemented cuts based on : target mask ; photo mag (g, r) ; chi2 from fit ; SNR (in spectra.scores, BRZ)
    '''
    
    # Target mask selection
    if mask is not None :
        assert mask_type in ['SV1_DESI_TARGET', 'DESI_TARGET', 'CMX_TARGET']
        if mask_type == 'SV1_DESI_TARGET' :
            assert ( mask in sv1_desi_mask.names() )            
            w, = np.where( (spectra.fibermap['SV1_DESI_TARGET'] & sv1_desi_mask[mask]) )
        elif mask_type == 'DESI_TARGET' :
            assert ( mask in desi_mask.names() ) 
            w, = np.where( (spectra.fibermap['DESI_TARGET'] & desi_mask[mask]) )
        elif mask_type == 'CMX_TARGET' :
            assert ( mask in cmx_mask.names() )
            w, = np.where( (spectra.fibermap['CMX_TARGET'] & cmx_mask[mask]) )                
        if len(w) == 0 :
            if log is not None : log.info(" * No spectra with mask "+mask)
            return 0
        else :
            targetids = spectra.fibermap['TARGETID'][w]
            spectra = myspecselect.myspecselect(spectra, targets=targetids)

    # Photometry selection
    if gmag_cut is not None :
        assert len(gmag_cut)==2 # Require range [gmin, gmax]
        gmag = np.zeros(spectra.num_spectra())
        w, = np.where( (spectra.fibermap['FLUX_G']>0) & (spectra.fibermap['MW_TRANSMISSION_G']>0) )
        gmag[w] = -2.5*np.log10(spectra.fibermap['FLUX_G'][w]/spectra.fibermap['MW_TRANSMISSION_G'][w])+22.5
        w, = np.where( (gmag>gmag_cut[0]) & (gmag<gmag_cut[1]) )
        if len(w) == 0 :
            if log is not None : log.info(" * No spectra with g_mag in requested range")
            return 0
        else :
            targetids = spectra.fibermap['TARGETID'][w]
            spectra = myspecselect.myspecselect(spectra, targets=targetids)
    if rmag_cut is not None :
        assert len(rmag_cut)==2 # Require range [rmin, rmax]
        rmag = np.zeros(spectra.num_spectra())
        w, = np.where( (spectra.fibermap['FLUX_R']>0) & (spectra.fibermap['MW_TRANSMISSION_R']>0) )
        rmag[w] = -2.5*np.log10(spectra.fibermap['FLUX_R'][w]/spectra.fibermap['MW_TRANSMISSION_R'][w])+22.5
        w, = np.where( (rmag>rmag_cut[0]) & (rmag<rmag_cut[1]) )
        if len(w) == 0 :
            if log is not None : log.info(" * No spectra with r_mag in requested range")
            return 0
        else :
            targetids = spectra.fibermap['TARGETID'][w]
            spectra = myspecselect.myspecselect(spectra, targets=targetids)

    # SNR selection ## TODO check it !! May not work ...
    if snr_cut is not None :
        assert ( (len(snr_cut)==2) and (spectra.scores is not None) )
        for band in ['B','R','Z'] :
            w, = np.where( (spectra.scores['MEDIAN_CALIB_SNR_'+band]>snr_cut[0]) & (spectra.scores['MEDIAN_CALIB_SNR_'+band]<snr_cut[1]) )
            if len(w) == 0 :
                if log is not None : log.info(" * No spectra with MEDIAN_CALIB_SNR_"+band+" in requested range")
                return 0
            else :
                targetids = spectra.fibermap['TARGETID'][w]
                spectra = myspecselect.myspecselect(spectra, targets=targetids)
    
    # Chi2 selection
    if chi2cut is not None :
        assert len(chi2cut)==2 # Require range [chi2min, chi2max]
        assert (zbest is not None)
        thezb, kk = match_zcat_to_spectra(zbest,spectra)
        w, = np.where( (thezb['DELTACHI2']>chi2cut[0]) & (thezb['DELTACHI2']<chi2cut[1]) )
        if len(w) == 0 :
            if log is not None : log.info(" * No target in this pixel with DeltaChi2 in requested range")
            return 0
        else :
            targetids = spectra.fibermap['TARGETID'][w]
            spectra = myspecselect.myspecselect(spectra, targets=targetids)

    return spectra


def _coadd(wave, flux, ivar, rdat):
    '''
    Return weighted coadd of spectra

    Parameters
    ----------
    wave : 1D[nwave] array of wavelengths
    flux : 2D[nspec, nwave] array of flux densities
    ivar : 2D[nspec, nwave] array of inverse variances of `flux`
    rdat : 3D[nspec, ndiag, nwave] sparse diagonals of resolution matrix

    Returns
    -------
        coadded spectrum (wave, outflux, outivar, outrdat)
    '''
    nspec, nwave = flux.shape
    unweightedflux = np.zeros(nwave, dtype=flux.dtype)
    weightedflux = np.zeros(nwave, dtype=flux.dtype)
    weights = np.zeros(nwave, dtype=flux.dtype)
    outrdat = np.zeros(rdat[0].shape, dtype=rdat.dtype)
    for i in range(nspec):
        unweightedflux += flux[i]
        weightedflux += flux[i] * ivar[i]
        weights += ivar[i]
        outrdat += rdat[i] * ivar[i]

    isbad = (weights == 0)
    outflux = weightedflux / (weights + isbad)
    outflux[isbad] = unweightedflux[isbad] / nspec

    outrdat /= (weights + isbad)
    outivar = weights

    return wave, outflux, outivar, outrdat

def coadd_targets(spectra, targetids=None):
    '''
    Coadds individual exposures of the same targets; returns new Spectra object

    Parameters
    ----------
    spectra : :class:`desispec.spectra.Spectra`
    targetids : (optional) array-like subset of target IDs to keep

    Returns
    -------
    coadded_spectra : :class:`desispec.spectra.Spectra` where individual
        spectra of each target have been combined into a single spectrum
        per camera.

    Note: coadds per camera but not across cameras.
    '''
    if targetids is None:
        targetids = spectra.target_ids()

    #- Create output arrays to fill
    ntargets = spectra.num_targets()
    wave = dict()
    flux = dict()
    ivar = dict()
    rdat = dict()
    if spectra.mask is None:
        mask = None
    else:
        mask = dict()
    for channel in spectra.bands:
        wave[channel] = spectra.wave[channel].copy()
        nwave = len(wave[channel])
        flux[channel] = np.zeros((ntargets, nwave))
        ivar[channel] = np.zeros((ntargets, nwave))
        ndiag = spectra.resolution_data[channel].shape[1]
        rdat[channel] = np.zeros((ntargets, ndiag, nwave))
        if mask is not None:
            mask[channel] = np.zeros((ntargets, nwave), dtype=spectra.mask[channel].dtype)

    #- Loop over targets, coadding all spectra for each target
    fibermap = Table(dtype=spectra.fibermap.dtype)
    for i, targetid in enumerate(targetids):
        ii = np.where(spectra.fibermap['TARGETID'] == targetid)[0]
        fibermap.add_row(spectra.fibermap[ii[0]])
        for channel in spectra.bands:
            if len(ii) > 1:
                outwave, outflux, outivar, outrdat = _coadd(
                    spectra.wave[channel],
                    spectra.flux[channel][ii],
                    spectra.ivar[channel][ii],
                    spectra.resolution_data[channel][ii]
                    )
                if mask is not None:
                    outmask = spectra.mask[channel][ii[0]]
                    for j in range(1, len(ii)):
                        outmask |= spectra.mask[channel][ii[j]]
            else:
                outwave, outflux, outivar, outrdat = (
                    spectra.wave[channel],
                    spectra.flux[channel][ii[0]],
                    spectra.ivar[channel][ii[0]],
                    spectra.resolution_data[channel][ii[0]]
                    )
                if mask is not None:
                    outmask = spectra.mask[channel][ii[0]]

            flux[channel][i] = outflux
            ivar[channel][i] = outivar
            rdat[channel][i] = outrdat
            if mask is not None:
                mask[channel][i] = outmask

    return desispec.spectra.Spectra(spectra.bands, wave, flux, ivar,
            mask=mask, resolution_data=rdat, fibermap=fibermap,
            meta=spectra.meta)


