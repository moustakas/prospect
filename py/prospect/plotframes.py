# -*- coding: utf-8 -*-


"""
TODO
* add target details tab
* add code details tab (version, SPECPROD)
* redshift model fit
* better smoothing kernel, e.g. gaussian
"""

import os, sys
import argparse

import numpy as np
import scipy.ndimage.filters

from astropy.table import Table
import astropy.io.fits

import bokeh.plotting as bk
from bokeh.models import ColumnDataSource, CDSView, IndexFilter
from bokeh.models import CustomJS, LabelSet, Label, Span, Legend, Panel, Tabs
from bokeh.models.widgets import (
    Slider, Button, Div, CheckboxGroup, CheckboxButtonGroup, RadioButtonGroup, 
    TextInput, Select, DataTable, TableColumn)
from bokeh.layouts import widgetbox, Spacer, gridplot
import bokeh.events
# from bokeh.layouts import row, column

import desispec.io
from desitarget.targetmask import desi_mask
from desitarget.cmx.cmx_targetmask import cmx_mask
from desitarget.sv1.sv1_targetmask import desi_mask as sv1_desi_mask
import desispec.spectra
import desispec.frame

#from . import utils_specviewer
from prospect import utils_specviewer
from prospect import mycoaddcam
from astropy.table import Table

def create_model(spectra, zbest):
    '''
    Returns model_wave[nwave], model_flux[nspec, nwave], row matched to zbest,
    which can be in a different order than spectra.
    NB currently, zbest must have the same size as spectra.
    '''
    import redrock.templates
    from desispec.interpolation import resample_flux

    nspec = spectra.num_spectra()
    assert len(zbest) == nspec

    #- Load redrock templates; redirect stdout because redrock is chatty
    saved_stdout = sys.stdout
    sys.stdout = open('/dev/null', 'w')
    try:
        templates = dict()
        for filename in redrock.templates.find_templates():
            tx = redrock.templates.Template(filename)
            templates[(tx.template_type, tx.sub_type)] = tx
    except Exception as err:
        sys.stdout = saved_stdout
        raise(err)

    sys.stdout = saved_stdout

    #- Empty model flux arrays per band to fill
    model_flux = dict()
    for band in spectra.bands:
        model_flux[band] = np.zeros(spectra.flux[band].shape)

    targetids = spectra.target_ids()
    for i in range(len(zbest)):
        zb = zbest[i]
        j = np.where(targetids == zb['TARGETID'])[0][0]

        tx = templates[(zb['SPECTYPE'], zb['SUBTYPE'])]
        coeff = zb['COEFF'][0:tx.nbasis]
        model = tx.flux.T.dot(coeff).T
        for band in spectra.bands:
            mx = resample_flux(spectra.wave[band], tx.wave*(1+zb['Z']), model)
            model_flux[band][i] = spectra.R[band][j].dot(mx)

    #- Now combine to a single wavelength grid across all cameras
    #- TODO: assumes b,r,z all exist
    assert np.all([ band in spectra.wave.keys() for band in ['b','r','z'] ])
    br_split = 0.5*(spectra.wave['b'][-1] + spectra.wave['r'][0])
    rz_split = 0.5*(spectra.wave['r'][-1] + spectra.wave['z'][0])
    keep = dict()
    keep['b'] = (spectra.wave['b'] < br_split)
    keep['r'] = (br_split <= spectra.wave['r']) & (spectra.wave['r'] < rz_split)
    keep['z'] = (rz_split <= spectra.wave['z'])
    model_wave = np.concatenate( [
        spectra.wave['b'][keep['b']],
        spectra.wave['r'][keep['r']],
        spectra.wave['z'][keep['z']],
    ] )

    mflux = np.concatenate( [
        model_flux['b'][:, keep['b']],
        model_flux['r'][:, keep['r']],
        model_flux['z'][:, keep['z']],
    ], axis=1 )

    return model_wave, mflux


def _viewer_urls(spectra, zoom=13, layer='dr8'):
    """Return legacysurvey.org viewer URLs for all spectra.
    """
    u = "http://legacysurvey.org/viewer/jpeg-cutout?ra={0:f}&dec={1:f}&zoom={2:d}&layer={3}"
    v = "http://legacysurvey.org/viewer/?ra={0:f}&dec={1:f}&zoom={2:d}&layer={3}"
    try:
        ra = spectra.fibermap['RA_TARGET']
        dec = spectra.fibermap['DEC_TARGET']
    except KeyError:
        ra = spectra.fibermap['TARGET_RA']
        dec = spectra.fibermap['TARGET_DEC']

    return [(u.format(ra[i], dec[i], zoom, layer),
             v.format(ra[i], dec[i], zoom, layer),
             'RA, Dec = {0:.4f}, {1:+.4f}'.format(ra[i], dec[i]))
            for i in range(len(ra))]


def make_cds_spectra(spectra, with_noise) :
    """ Creates column data source for b,r,z observed spectra """

    cds_spectra = list()
    for band in spectra.bands:
        cdsdata=dict(
            origwave=spectra.wave[band].copy(),
            plotwave=spectra.wave[band].copy(),
            )
        for i in range(spectra.num_spectra()):
            key = 'origflux'+str(i)
            cdsdata[key] = spectra.flux[band][i]
            if with_noise :
                key = 'orignoise'+str(i)
                noise = np.zeros(len(spectra.ivar[band][i]))
                w, = np.where( (spectra.ivar[band][i] > 0))
                noise[w] = 1/np.sqrt(spectra.ivar[band][i][w])
                cdsdata[key] = noise
        cdsdata['plotflux'] = cdsdata['origflux0']
        if with_noise : cdsdata['plotnoise'] = cdsdata['orignoise0'] 
        cds_spectra.append( bk.ColumnDataSource(cdsdata, name=band) )
    
    return cds_spectra

def make_cds_coaddcam_spec(spectra, with_noise) :
    """ Creates column data source for camera-coadded observed spectra 
        Do NOT store all coadded spectra in CDS obj, to reduce size of html files
        Except for the first spectrum, coaddition is done later in javascript
    """

    coadd_wave, coadd_flux, coadd_ivar = mycoaddcam.mycoaddcam(spectra)
    cds_coaddcam_data = dict(
        origwave = coadd_wave.copy(),
        plotwave = coadd_wave.copy(),
        plotflux = coadd_flux[0,:].copy(),
        plotnoise = np.ones(len(coadd_wave))
    )
    if with_noise :
        w, = np.where( (coadd_ivar[0,:] > 0) )
        cds_coaddcam_data['plotnoise'][w] = 1/np.sqrt(coadd_ivar[0,:][w])
    cds_coaddcam_spec = bk.ColumnDataSource(cds_coaddcam_data)
    
    return cds_coaddcam_spec

def make_cds_model(model) :
    """ Creates column data source for model spectrum """
    
    mwave, mflux = model
    cds_model_data = dict(
        origwave = mwave.copy(),
        plotwave = mwave.copy(),
        plotflux = np.zeros(len(mwave)),
    )
    for i in range(len(mflux)):
        key = 'origflux'+str(i)
        cds_model_data[key] = mflux[i]

    cds_model_data['plotflux'] = cds_model_data['origflux0']
    cds_model = bk.ColumnDataSource(cds_model_data)

    return cds_model

def make_cds_targetinfo(spectra, zcatalog, is_coadded, mask_type, username=" ") :
    """ Creates column data source for target-related metadata, from zcatalog, fibermap and VI files """

    assert mask_type in ['SV1_DESI_TARGET', 'DESI_TARGET', 'CMX_TARGET']
    target_info = list()
    vi_info = list()
    for i, row in enumerate(spectra.fibermap):
        if mask_type == 'SV1_DESI_TARGET' :
            target_bit_names = ' '.join(sv1_desi_mask.names(row['SV1_DESI_TARGET']))
        elif mask_type == 'DESI_TARGET' :
            target_bit_names = ' '.join(desi_mask.names(row['DESI_TARGET']))
        elif mask_type == 'CMX_TARGET' :
            target_bit_names = ' '.join(cmx_mask.names(row['CMX_TARGET']))
        txt = 'TargetID {}: {} '.format(row['TARGETID'], target_bit_names)
        if not is_coadded :
            ## BYPASS DIV
            #           txt += '<BR />'
            if 'NIGHT' in spectra.fibermap.keys() : txt += "Night : {}".format(row['NIGHT'])
            if 'EXPID' in spectra.fibermap.keys() : txt += "Exposure : {}".format(row['EXPID'])
            if 'FIBER' in spectra.fibermap.keys() : txt += "Fiber : {}".format(row['FIBER'])
## BYPASS DIV
#         if (row['FLUX_G'] > 0 and row['MW_TRANSMISSION_G'] > 0) :
#             gmag = -2.5*np.log10(row['FLUX_G']/row['MW_TRANSMISSION_G'])+22.5
#         else : gmag = 0
#         txt += '<BR /> Photometry (dereddened) : g<SUB>mag</SUB>={:.1f}'.format(gmag)
## BYPASS DIV
#         if zcatalog is not None:
#             txt += '<BR /> Fit result : {} z={:.4f} ± {:.4f}&emsp;&emsp; z<SUB>WARN</SUB>={}&emsp;&emsp; &Delta;&chi;<SUP>2</SUP>={:.1f}'.format(
#                 zcatalog['SPECTYPE'][i],
#                 zcatalog['Z'][i],
#                 zcatalog['ZERR'][i],
#                 zcatalog['ZWARN'][i],
#                 zcatalog['DELTACHI2'][i]
#             )
        target_info.append(txt)
        # TMP no vidata (will change it)
#         if ( (vidata is not None) and (len(vidata[i])>0) ) :
#             txt = ('<BR/> VI info : SCANNER FLAG COMMENTS')
#             for the_vi in vidata[i] :
#                 txt += ('<BR/>&emsp;&emsp;&emsp;&emsp; {0} {1} {2}'.format(the_vi['scannername'], the_vi['scanflag'], the_vi['VIcomment']))
#        else : 
        txt = ('<BR/> No VI previously recorded for this target')
        vi_info.append(txt)

    cds_targetinfo = bk.ColumnDataSource(
        dict(target_info=target_info),
        name='target_info')
    cds_targetinfo.add(vi_info, name='vi_info')
    
    ## BYPASS DIV : Added photometry fields ; also add several bands
    bands = ['G','R','Z', 'W1', 'W2']
    for bandname in bands :
        mag = np.zeros(spectra.num_spectra())
        flux = spectra.fibermap['FLUX_'+bandname]
        extinction = np.ones(len(flux))
        if ('MW_TRANSMISSION_'+bandname) in spectra.fibermap.keys() :
            extinction = spectra.fibermap['MW_TRANSMISSION_'+bandname]
        w, = np.where( (flux>0) & (extinction>0) )
        mag[w] = -2.5*np.log10(flux[w]/extinction[w])+22.5
        cds_targetinfo.add(mag, name='mag_'+bandname)
    
    if zcatalog is not None:
        cds_targetinfo.add(zcatalog['Z'], name='z')
        cds_targetinfo.add(zcatalog['SPECTYPE'].astype('U{0:d}'.format(zcatalog['SPECTYPE'].dtype.itemsize)), name='spectype')
        # BYPASS DIV : Added fields
        cds_targetinfo.add(zcatalog['ZERR'], name='zerr')
        cds_targetinfo.add(zcatalog['ZWARN'], name='zwarn')
        cds_targetinfo.add(zcatalog['DELTACHI2'], name='deltachi2')

    nspec = spectra.num_spectra()
    if not is_coadded and 'EXPID' in spectra.fibermap.keys() :
        cds_targetinfo.add(spectra.fibermap['EXPID'], name='expid')
    else : # If coadd, fill VI accordingly
        cds_targetinfo.add(['-1' for i in range(nspec)], name='expid')
    cds_targetinfo.add([str(x) for x in spectra.fibermap['TARGETID']], name='targetid') # !! No int64 in js !!

    #- FIXME: should not hardcode which DEPVERnn has which versions
    ### cds_targetinfo.add([spectra.meta['DEPVER10'] for i in range(nspec)], name='spec_version')
    ### cds_targetinfo.add([spectra.meta['DEPVER13'] for i in range(nspec)], name='redrock_version')
    cds_targetinfo.add(np.zeros(nspec), name='spec_version')
    cds_targetinfo.add(np.zeros(nspec), name='redrock_version')

    # VI inputs
    cds_targetinfo.add([username for i in range(nspec)], name='VI_scanner')
    cds_targetinfo.add(["-1" for i in range(nspec)], name='VI_class_flag') 
    cds_targetinfo.add([" " for i in range(nspec)], name='VI_issue_flag')
    cds_targetinfo.add([" " for i in range(nspec)], name='VI_z')
    cds_targetinfo.add([" " for i in range(nspec)], name='VI_spectype')
    cds_targetinfo.add([" " for i in range(nspec)], name='VI_comment')
    
    return cds_targetinfo


def grid_thumbs(spectra, thumb_width, x_range=(3400,10000), thumb_height=None, resamp_factor=15, ncols_grid=5, titles=None) :
    '''
    Create a bokeh gridplot of thumbnail pictures from spectra
    - coadd arms
    - smooth+resample to reduce size of embedded CDS, according to resamp_factor
    - titles : optional list of titles for each thumb
    '''

    if thumb_height is None : thumb_height = thumb_width//2
    if titles is not None : assert len(titles) == spectra.num_spectra()
    thumb_wave, thumb_flux, dummy = mycoaddcam.mycoaddcam(spectra)
    
    thumb_plots = []
    for i_spec in range(spectra.num_spectra()) :
        # other option use CustomJSTransform ?
        # (https://docs.bokeh.org/en/1.1.0/docs/user_guide/data.html)
        x_vals = (thumb_wave[::resamp_factor])[resamp_factor:-resamp_factor]
        y_vals = scipy.ndimage.filters.gaussian_filter1d(thumb_flux[i_spec,:], sigma=resamp_factor, mode='nearest') 
        y_vals = (y_vals[::resamp_factor])[resamp_factor:-resamp_factor]
        x_vals = x_vals[~np.isnan(y_vals)] # TODO - should we keep that in the end ?
        y_vals = y_vals[~np.isnan(y_vals)]            
        yampl = np.max(y_vals) - np.min(y_vals)
        ymin = np.min(y_vals) - 0.1*yampl
        ymax = np.max(y_vals) + 0.1*yampl
        plot_title = None
        if titles is not None : plot_title = titles[i_spec]
        mini_plot = bk.figure(plot_width=thumb_width, plot_height=thumb_height, x_range=x_range, y_range=(ymin,ymax), title=plot_title)
        mini_plot.line(x_vals, y_vals, line_color='red')
        mini_plot.xaxis.visible = False
        mini_plot.yaxis.visible = False
        mini_plot.min_border_left = 0
        mini_plot.min_border_right = 0
        mini_plot.min_border_top = 0
        mini_plot.min_border_bottom = 0
        thumb_plots.append(mini_plot)

    return gridplot(thumb_plots, ncols=ncols_grid, toolbar_location=None, sizing_mode='scale_width')


def plotspectra(spectra, nspec=None, startspec=None, zcatalog=None, model_from_zcat=True, model=None, notebook=False, vidata=None, is_coadded=True, title=None, html_dir=None, with_imaging=True, with_noise=True, with_coaddcam=True, mask_type='DESI_TARGET', with_thumb_tab=True, with_vi_widgets=True, with_thumb_only_page=False):
    '''
    Main prospect routine, creates a bokeh document from a set of spectra and fits

    Parameter
    ---------
    spectra : desi spectra object, or a list of frames
    nspec : select subsample of spectra, only for frame input
    startspec : if nspec is set, subsample selection will be [startspec:startspec+nspec]
    zcatalog : FITS file of pipeline redshifts for the spectra. Currently supports only redrock-PCA files.
    model_from_zcat : if True, model spectra will be computed from the input zcatalog
    model : if set, use this input set of model spectra (instead of computing it from zcat)
        model format (mwave, mflux); model must be entry-matched to zcatalog.
    notebook : if True, bokeh outputs the viewer to notebook, else to a (static) html page
    vidata : VI information to be preloaded and displayed. Currently disabled.
    is_coadded : set to True if spectra are coadds
    title : title used to produce html page / name bokeh figure / save VI file
    html_dir : directory to store html page
    with_imaging : include thumb image from legacysurvey.org
    with_noise : include noise for each spectrum
    with_coaddcam : include camera-coaddition
    with_thumb_tab : include tab with thumbnails of spectra in viewer
    with_vi_widgets : include widgets used to enter VI informations
    with_thumb_only_page (requires notebook==False) : also create a light html page including only the thumb gallery
    mask_type : mask type to identify target categories from the fibermap. Available : DESI_TARGET,
        SV1_DESI_TARGET, CMX_TARGET. Default : DESI_TARGET.
    '''

    #- If inputs are frames, convert to a spectra object
    if isinstance(spectra, list) and isinstance(spectra[0], desispec.frame.Frame):
        spectra = utils_specviewer.frames2spectra(spectra, nspec=nspec, startspec=startspec)
        frame_input = True
    else:
        frame_input = False
        assert nspec is None
    nspec = spectra.num_spectra() # NB can be less than input "nspec"
    #- Set masked bins to NaN so that Bokeh won't plot them
    for band in spectra.bands:
        bad = (spectra.ivar[band] == 0.0) | (spectra.mask[band] != 0)
        spectra.flux[band][bad] = np.nan

    if frame_input and title is None:
        meta = spectra.meta
        title = 'Night {} ExpID {} Spectrograph {}'.format(
            meta['NIGHT'], meta['EXPID'], meta['CAMERA'][1],
        )
    if title is None : title = "specviewer"

    #- Reorder zcatalog to match input targets
    #- TODO: allow more than one zcatalog entry with different ZNUM per targetid
    if zcatalog is not None:
        zcatalog, kk = utils_specviewer.match_zcat_to_spectra(zcatalog, spectra)
        
        #- Also need to re-order input model fluxes
        if model is not None :
            assert model_from_zcat == False
            mwave, mflux = model
            model = mwave, mflux[kk]

        if model_from_zcat == True :
            model = create_model(spectra, zcatalog)

    #-----
    #- Initialize Bokeh output
    if notebook:
        assert with_thumb_only_page == False
        bk.output_notebook()
    else :
        if html_dir is None : raise RuntimeError("Need html_dir")
        html_page = os.path.join(html_dir, "specviewer_"+title+".html")
        bk.output_file(html_page, title='DESI spectral viewer')

    #-----
    #- Gather information into ColumnDataSource objects for Bokeh
    cds_spectra = make_cds_spectra(spectra, with_noise)
    if with_coaddcam :
        cds_coaddcam_spec = make_cds_coaddcam_spec(spectra, with_noise)
    else :
        cds_coaddcam_spec = None
    if model is not None:
        cds_model = make_cds_model(model)
    else:
        cds_model = None
    if notebook and ("USER" in os.environ) : 
        username = os.environ['USER']
    else :
        username = " "
    cds_targetinfo = make_cds_targetinfo(spectra, zcatalog, is_coadded, mask_type, username=username)


    #-------------------------
    #-- Graphical objects --
    #-------------------------


    #-----
    #- Main figure
    #- Determine initial ymin, ymax, xmin, xmax
    ymin = ymax = xmax = 0.0
    xmin = 100000.
    xmargin = 300.
    for band in spectra.bands:
        ymin = min(ymin, np.nanmin(spectra.flux[band][0]))
        ymax = max(ymax, np.nanmax(spectra.flux[band][0]))
        xmin = min(xmin, np.min(spectra.wave[band]))
        xmax = max(xmax, np.max(spectra.wave[band]))        
    xmin -= xmargin
    xmax += xmargin
    
    plot_width=800
    plot_height=400
    tools = 'pan,box_zoom,wheel_zoom,save'
    tooltips_fig = [("wave","$x"),("flux","$y")]
    fig = bk.figure(height=plot_height, width=plot_width, title=title,
        tools=tools, toolbar_location='above', tooltips=tooltips_fig, y_range=(ymin, ymax), x_range=(xmin, xmax))
    fig.sizing_mode = 'stretch_width'
    fig.toolbar.active_drag = fig.tools[0]    #- pan zoom (previously box)
    fig.toolbar.active_scroll = fig.tools[2]  #- wheel zoom
    fig.xaxis.axis_label = 'Wavelength [Å]'
    fig.yaxis.axis_label = 'Flux'
    fig.xaxis.axis_label_text_font_style = 'normal'
    fig.yaxis.axis_label_text_font_style = 'normal'
    colors = dict(b='#1f77b4', r='#d62728', z='maroon', coadd='#d62728')
    noise_colors = dict(b='greenyellow', r='green', z='forestgreen', coadd='green') # TODO test several and choose
    alpha_discrete = 0.2 # alpha for "almost-hidden" curves (single-arm spectra and noise by default)
    if not with_coaddcam : alpha_discrete = 1
    
    data_lines = list()
    for spec in cds_spectra:
        lx = fig.line('plotwave', 'plotflux', source=spec, line_color=colors[spec.name], line_alpha=alpha_discrete)
        data_lines.append(lx)
    if with_coaddcam :
        lx = fig.line('plotwave', 'plotflux', source=cds_coaddcam_spec, line_color=colors['coadd'], line_alpha=1)
        data_lines.append(lx)
    
    noise_lines = list()
    if with_noise :
        for spec in cds_spectra :
            lx = fig.line('plotwave', 'plotnoise', source=spec, line_color=noise_colors[spec.name], line_alpha=alpha_discrete)
            noise_lines.append(lx)
        if with_coaddcam :
            lx = fig.line('plotwave', 'plotnoise', source=cds_coaddcam_spec, line_color=noise_colors['coadd'], line_alpha=1)
            noise_lines.append(lx)

    model_lines = list()
    if cds_model is not None:
        lx = fig.line('plotwave', 'plotflux', source=cds_model, line_color='black')
        model_lines.append(lx)

    legend_items = [("data",  data_lines[-1::-1])] #- reversed to get blue as lengend entry
    if cds_model is not None : 
        legend_items.append(("model", model_lines))
    if with_noise : 
        legend_items.append(("noise", noise_lines[-1::-1])) # same as for data_lines
    legend = Legend(items=legend_items)

    fig.add_layout(legend, 'center')
    fig.legend.click_policy = 'hide'    #- or 'mute'

    #-----
    #- Zoom figure around mouse hover of main plot
    tooltips_zoomfig = [("wave","$x"),("flux","$y")]
    zoomfig = bk.figure(height=plot_height//2, width=plot_height//2,
        y_range=fig.y_range, x_range=(5000,5100),
        # output_backend="webgl",
        toolbar_location=None, tooltips=tooltips_zoomfig, tools=[])

    zoom_data_lines = list()
    zoom_noise_lines = list()
    for spec in cds_spectra:
        zoom_data_lines.append(zoomfig.line('plotwave', 'plotflux', source=spec,
            line_color=colors[spec.name], line_width=1, line_alpha=alpha_discrete))
        if with_noise :
            zoom_noise_lines.append(zoomfig.line('plotwave', 'plotnoise', source=spec,
                            line_color=noise_colors[spec.name], line_width=1, line_alpha=alpha_discrete))
    if with_coaddcam :
        zoom_data_lines.append(zoomfig.line('plotwave', 'plotflux', source=cds_coaddcam_spec, line_color=colors['coadd'], line_alpha=1))
        if with_noise :
            lx = zoomfig.line('plotwave', 'plotnoise', source=cds_coaddcam_spec, line_color=noise_colors['coadd'], line_alpha=1)
            zoom_noise_lines.append(lx)
            
    zoom_model_lines = list()
    if cds_model is not None:
        zoom_model_lines.append(zoomfig.line('plotwave', 'plotflux', source=cds_model, line_color='black'))

    #- Callback to update zoom window x-range
    zoom_callback = CustomJS(
        args=dict(zoomfig=zoomfig,fig=fig),
        code="""
            zoomfig.x_range.start = cb_obj.x - 100;
            zoomfig.x_range.end = cb_obj.x + 100;
        """)

    fig.js_on_event(bokeh.events.MouseMove, zoom_callback)

    #-----
    #- Targeting image
    if with_imaging :
        imfig = bk.figure(width=plot_height//2, height=plot_height//2,
                          x_range=(0, 256), y_range=(0, 256),
                          x_axis_location=None, y_axis_location=None,
                          output_backend="webgl",
                          toolbar_location=None, tools=[])
        imfig.min_border_left = 0
        imfig.min_border_right = 0
        imfig.min_border_top = 0
        imfig.min_border_bottom = 0

        imfig_urls = _viewer_urls(spectra)
        imfig_source = ColumnDataSource(data=dict(url=[imfig_urls[0][0]],
                                                  txt=[imfig_urls[0][2]]))

        imfig_img = imfig.image_url('url', source=imfig_source, x=1, y=1, w=256, h=256, anchor='bottom_left')
        imfig_txt = imfig.text(10, 256-30, text='txt', source=imfig_source,
                               text_color='yellow', text_font_size='8pt')
    else : 
        imfig = Spacer(width=plot_height//2, height=plot_height//2)
        imfig_source = imfig_urls = None

    #-----
    #- Emission and absorption lines
    z = zcatalog['Z'][0] if (zcatalog is not None) else 0.0
    line_data, lines, line_labels = add_lines(fig, z=z)
    zoom_line_data, zoom_lines, zoom_line_labels = add_lines(zoomfig, z=z, label_offsets=[50, 5])


    #-------------------------
    #-- Widgets and callbacks --
    #-------------------------

    js_dir = os.path.join(os.path.dirname(__file__),os.pardir,os.pardir,"js")

    #-----
    #- Ifiberslider and smoothing widgets
    # Ifiberslider's value controls which spectrum is displayed
    # These two widgets call update_plot(), later defined
    slider_end = nspec-1 if nspec > 1 else 0.5 # Slider cannot have start=end
    ifiberslider = Slider(start=0, end=slider_end, value=0, step=1, title='Spectrum')
    smootherslider = Slider(start=0, end=51, value=0, step=1.0, title='Gaussian Sigma Smooth')

    #-----
    #- Navigation buttons
    navigation_button_width = 30
    prev_button = Button(label="<", width=navigation_button_width)
    next_button = Button(label=">", width=navigation_button_width)
    prev_callback = CustomJS(
        args=dict(ifiberslider=ifiberslider),
        code="""
        if(ifiberslider.value>0 && ifiberslider.end>=1) {
            ifiberslider.value--
        }
        """)
    next_callback = CustomJS(
        args=dict(ifiberslider=ifiberslider, nspec=nspec),
        code="""
        if(ifiberslider.value<nspec-1 && ifiberslider.end>=1) {
            ifiberslider.value++
        }
        """)
    prev_button.js_on_event('button_click', prev_callback)
    next_button.js_on_event('button_click', next_callback)


    #-----
    #- Axis reset button (superseeds the default bokeh "reset"
    reset_plotrange_button = Button(label="Reset X-Y range",button_type="default")
    reset_plotrange_callback = CustomJS(args = dict(fig=fig, xmin=xmin, xmax=xmax, spectra=cds_spectra), code="""
        // x-range : use fixed x-range determined once for all
        fig.x_range.start = xmin
        fig.x_range.end = xmax
        
        // y-range : same function as in update_plot()
        function get_y_minmax(pmin, pmax, data) {
            var dx = data.slice().filter(Boolean)
            dx.sort()
            var imin = Math.floor(pmin * dx.length)
            var imax = Math.floor(pmax * dx.length)
            return [dx[imin], dx[imax]]
        }
        var ymin = 0.0
        var ymax = 0.0
        for (var i=0; i<spectra.length; i++) {
            var data = spectra[i].data
            tmp = get_y_minmax(0.01, 0.99, data['plotflux'])
            ymin = Math.min(ymin, tmp[0])
            ymax = Math.max(ymax, tmp[1])
        }
        if(ymin<0) {
            fig.y_range.start = ymin * 1.4
        } else {
            fig.y_range.start = ymin * 0.6
        }
        fig.y_range.end = ymax * 1.4

    """)
    reset_plotrange_button.js_on_event('button_click', reset_plotrange_callback)

    #-----
    #- Redshift / wavelength scale widgets
    z1 = np.floor(z*100)/100
    dz = z-z1
    zslider = Slider(start=0.0, end=4.0, value=z1, step=0.01, title='Redshift rough tuning')
    dzslider = Slider(start=-0.01, end=0.01, value=dz, step=0.0001, title='Redshift fine-tuning')
    dzslider.format = "0[.]0000"
    zdisp_cds = bk.ColumnDataSource(dict(z_disp=[ "{:.4f}".format(z+dz) ]), name='zdisp_cds')
    zdisp_cols = [ TableColumn(field="z_disp", title="z_disp") ]
    z_display = DataTable(source=zdisp_cds, columns=zdisp_cols, index_position=None, width=70, selectable=False)
    z_display.height = 2 * z_display.row_height
    #    z_display = Div(text="<b>z<sub>disp</sub> = "+("{:.4f}").format(z+dz)+"</b>") ## Using Div is slow !!

    #- Observer vs. Rest frame wavelengths
    waveframe_buttons = RadioButtonGroup(
        labels=["Obs", "Rest"], active=0)

    zslider_callback  = CustomJS(
        args=dict(
            spectra = cds_spectra,
            coaddcam_spec = cds_coaddcam_spec,
            model = cds_model,
            targetinfo = cds_targetinfo,
            ifiberslider = ifiberslider,
            zslider=zslider,
            dzslider=dzslider,
#            z_display = z_display,
            zdisp_cds = zdisp_cds,
            waveframe_buttons=waveframe_buttons,
            line_data=line_data, lines=lines, line_labels=line_labels,
            zlines=zoom_lines, zline_labels=zoom_line_labels,
            fig=fig,
            ),
        code="""
        var z = zslider.value + dzslider.value
//        z_display.text = "<b>z<sub>disp</sub> = " + z.toFixed(4) + "</b>"
        zdisp_cds.data['z_disp']=[ z.toFixed(4) ]
        zdisp_cds.change.emit()

        var line_restwave = line_data.data['restwave']
        var ifiber = ifiberslider.value
        var zfit = 0.0
        if(targetinfo.data['z'] != undefined) {
            zfit = targetinfo.data['z'][ifiber]
        }
        var waveshift_lines = (waveframe_buttons.active == 0) ? 1+z : 1 ;
        for(var i=0; i<line_restwave.length; i++) {
            lines[i].location = line_restwave[i] * waveshift_lines
            line_labels[i].x = line_restwave[i] * waveshift_lines
            zlines[i].location = line_restwave[i] * waveshift_lines
            zline_labels[i].x = line_restwave[i] * waveshift_lines
        }
        function shift_plotwave(cds_spec, waveshift) {
            var data = cds_spec.data
            var origwave = data['origwave']
            var plotwave = data['plotwave']
            if ( plotwave[0] != origwave[0] * waveshift ) { // Avoid redo calculation if not needed
                for (var j=0; j<plotwave.length; j++) {
                    plotwave[j] = origwave[j] * waveshift ;
                }
                cds_spec.change.emit()
            }
        }
        
        var waveshift_spec = (waveframe_buttons.active == 0) ? 1 : 1/(1+z) ;
        for(var i=0; i<spectra.length; i++) {
            shift_plotwave(spectra[i], waveshift_spec)
        }
        if (coaddcam_spec) shift_plotwave(coaddcam_spec, waveshift_spec)
        
        // Update model wavelength array
        if(model) {
            var waveshift_model = (waveframe_buttons.active == 0) ? (1+z)/(1+zfit) : 1/(1+zfit) ;
            shift_plotwave(model, waveshift_model)
        }
        """)

    zslider.js_on_change('value', zslider_callback)
    dzslider.js_on_change('value', zslider_callback)
    waveframe_buttons.js_on_click(zslider_callback)

    zreset_button = Button(label='Reset redshift')
    zreset_callback = CustomJS(
        args=dict(zslider=zslider, dzslider=dzslider, targetinfo=cds_targetinfo, ifiberslider=ifiberslider),
        code="""
            var ifiber = ifiberslider.value
            var z = targetinfo.data['z'][ifiber]
            var z1 = Math.floor(z*100) / 100
            zslider.value = z1
            dzslider.value = (z - z1)
        """)
    zreset_button.js_on_event('button_click', zreset_callback)

    plotrange_callback = CustomJS(
        args = dict(
            zslider=zslider,
            dzslider=dzslider,
            waveframe_buttons=waveframe_buttons,
            fig=fig,
        ),
        code="""
        var z = zslider.value + dzslider.value
        // Observer Frame
        if(waveframe_buttons.active == 0) {
            fig.x_range.start = fig.x_range.start * (1+z)
            fig.x_range.end = fig.x_range.end * (1+z)
        } else {
            fig.x_range.start = fig.x_range.start / (1+z)
            fig.x_range.end = fig.x_range.end / (1+z)
        }
        """
    )
    waveframe_buttons.js_on_click(plotrange_callback)


    #-----
    #- Targeting image callback
    if with_imaging :
        imfig_callback = CustomJS(args=dict(urls=imfig_urls,
                                            ifiberslider=ifiberslider),
                                  code='''window.open(urls[ifiberslider.value][1], "_blank");''')
        imfig.js_on_event('tap', imfig_callback)

   
    #-----
    #- Checkboxes to display noise / model
    disp_opt_labels = []
    if cds_model is not None : disp_opt_labels.append('Show model')
    if with_noise : disp_opt_labels.append('Show noise spectra')
    display_options_group = CheckboxGroup(labels=disp_opt_labels, 
                                          active=list(range(len(disp_opt_labels))))
    disp_opt_callback = CustomJS(
        args = dict(noise_lines=noise_lines, model_lines=model_lines, zoom_noise_lines=zoom_noise_lines, zoom_model_lines=zoom_model_lines), code="""
        var i_noise = cb_obj.labels.indexOf("Show noise spectra")
        if (i_noise >= 0) {
            for (var i=0; i<noise_lines.length; i++) {
                if (cb_obj.active.indexOf(i_noise) >= 0) {
                    noise_lines[i].visible = true
                    zoom_noise_lines[i].visible = true
                } else {
                    noise_lines[i].visible = false
                    zoom_noise_lines[i].visible = false
                }
            }
        }
        var i_model = cb_obj.labels.indexOf("Show model")
        if (i_model >= 0) {
            for (var i=0; i<model_lines.length; i++) {
                if (cb_obj.active.indexOf(i_model) >= 0) {
                    model_lines[i].visible = true
                    zoom_model_lines[i].visible = true
                } else {
                    model_lines[i].visible = false
                    zoom_model_lines[i].visible = false
                }
            }
        }
        """
    )
    display_options_group.js_on_click(disp_opt_callback)
    
    #-----
    #- Highlight individual-arm or camera-coadded spectra
    coaddcam_labels = []
    if cds_coaddcam_spec is not None : coaddcam_labels = ["Camera-coadded", "Single-arm"]
    coaddcam_buttons = RadioButtonGroup(labels=coaddcam_labels, active=0)
    coaddcam_callback = CustomJS(
        args = dict(coaddcam_buttons=coaddcam_buttons, list_lines=[data_lines, noise_lines, zoom_data_lines, zoom_noise_lines], alpha_discrete=alpha_discrete), code="""
        var n_lines = list_lines[0].length
        for (var i=0; i<n_lines; i++) {
            var new_alpha = 1
            if (coaddcam_buttons.active == 0 && i<n_lines-1) new_alpha = alpha_discrete
            if (coaddcam_buttons.active == 1 && i==n_lines-1) new_alpha = alpha_discrete
            for (var j=0; j<list_lines.length; j++) {
                list_lines[j][i].glyph.line_alpha = new_alpha
            }
        }
        """
    )
    coaddcam_buttons.js_on_click(coaddcam_callback)

    #-----
    # Display object-related informations
    ## BYPASS DIV
#    target_info_div = Div(text=cds_targetinfo.data['target_info'][0])
    tmp_dict = dict()
    tmp_dict['TARGETING'] = [ cds_targetinfo.data['target_info'][0] ]
    targ_disp_cols = [ TableColumn(field='TARGETING', title='TARGETING', width=plot_width-120-50-5*50) ] # TODO non-hardcode width
    for band in ['G', 'R', 'Z', 'W1', 'W2'] :
        tmp_dict['mag_'+band] = [ "{:.2f}".format(cds_targetinfo.data['mag_'+band][0]) ]
        targ_disp_cols.append( TableColumn(field='mag_'+band, title='mag_'+band, width=50) )
    targ_disp_cds = bk.ColumnDataSource(tmp_dict, name='targ_disp_cds')
    targ_display = DataTable(source = targ_disp_cds, columns=targ_disp_cols,index_position=None, selectable=False) # width=...
    targ_display.height = 2 * targ_display.row_height
    if zcatalog is not None :
        tmp_dict = dict(SPECTYPE = [ cds_targetinfo.data['spectype'][0] ],
                        Z = [ "{:.4f}".format(cds_targetinfo.data['z'][0]) ],
                        ZERR = [ "{:.4f}".format(cds_targetinfo.data['zerr'][0]) ],
                        ZWARN = [ cds_targetinfo.data['zwarn'][0] ],
                        DeltaChi2 = [ "{:.1f}".format(cds_targetinfo.data['deltachi2'][0]) ])
        zcat_disp_cds = bk.ColumnDataSource(tmp_dict, name='zcat_disp_cds')
        zcat_disp_cols = [ TableColumn(field=x, title=x, width=w) for x,w in [ ('SPECTYPE',100), ('Z',50) , ('ZERR',50), ('ZWARN',50), ('DeltaChi2',50) ] ]
        zcat_display = DataTable(source=zcat_disp_cds, columns=zcat_disp_cols, index_position=None, selectable=False, width=400) # width=...
        zcat_display.height = 2 * zcat_display.row_height
    else :
        zcat_display = Div(text="Not available ")
        zcat_disp_cds = None
        
    vi_info_div = Div(text=" ") # consistent with show_prev_vi="No" by default

    #-----
    #- Toggle lines
    lines_button_group = CheckboxButtonGroup(
            labels=["Emission", "Absorption"], active=[])
    majorline_checkbox = CheckboxGroup(
            labels=['Show only major lines'], active=[])

    lines_callback = CustomJS(
        args = dict(line_data=line_data, lines=lines, line_labels=line_labels, zlines=zoom_lines, 
                    zline_labels=zoom_line_labels, lines_button_group=lines_button_group, majorline_checkbox=majorline_checkbox),
        code="""
        var show_emission = false
        var show_absorption = false
        if (lines_button_group.active.indexOf(0) >= 0) {  // index 0=Emission in active list
            show_emission = true
        }
        if (lines_button_group.active.indexOf(1) >= 0) {  // index 1=Absorption in active list
            show_absorption = true
        }

        for(var i=0; i<lines.length; i++) {
            if ( !(line_data.data['major'][i]) && (majorline_checkbox.active.indexOf(0)>=0) ) {
                lines[i].visible = false
                line_labels[i].visible = false
                zlines[i].visible = false
                zline_labels[i].visible = false
            } else if (line_data.data['emission'][i]) {
                lines[i].visible = show_emission
                line_labels[i].visible = show_emission
                zlines[i].visible = show_emission
                zline_labels[i].visible = show_emission
            } else {
                lines[i].visible = show_absorption
                line_labels[i].visible = show_absorption
                zlines[i].visible = show_absorption
                zline_labels[i].visible = show_absorption
            }
        }
        """
    )
    lines_button_group.js_on_click(lines_callback)
    majorline_checkbox.js_on_click(lines_callback)

    #-----
    #- VI-related widgets
    
    vi_file_fields = utils_specviewer._vi_file_fields
    vi_class_labels = [ x["label"] for x in utils_specviewer._vi_flags if x["type"]=="class" ]
    vi_issue_labels = [ x["label"] for x in utils_specviewer._vi_flags if x["type"]=="issue" ]
    vi_issue_slabels = [ x["shortlabel"] for x in utils_specviewer._vi_flags if x["type"]=="issue" ]

    #- VI file name
    default_vi_filename = "desi-vi_"+title
    if username.strip()!="" :
        default_vi_filename += ("_"+username)
    else :
        default_vi_filename += "_unknown-user"
    default_vi_filename += ".csv"
    vi_filename_input = TextInput(value=default_vi_filename, title="VI file name :")
    
    #- Main VI classification
    vi_class_input = RadioButtonGroup(labels=vi_class_labels)
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_class_code = f.read()
    vi_class_code += """
        if ( vi_class_input.active >= 0 ) {
            cds_targetinfo.data['VI_class_flag'][ifiberslider.value] = vi_class_labels[vi_class_input.active]
        } else {
            cds_targetinfo.data['VI_class_flag'][ifiberslider.value] = "-1"
        }
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        cds_targetinfo.change.emit()
    """
    vi_class_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, vi_class_input=vi_class_input, 
                vi_class_labels=vi_class_labels, ifiberslider = ifiberslider,
                title=title, vi_file_fields = vi_file_fields), 
        code=vi_class_code )
    vi_class_input.js_on_click(vi_class_callback)

    #- Optional VI flags (issues)
    vi_issue_input = CheckboxGroup(labels=vi_issue_labels, active=[])
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_issue_code = f.read()
    vi_issue_code += """
        var issues = []
        for (var i=0; i<vi_issue_labels.length; i++) {
            if (vi_issue_input.active.indexOf(i) >= 0) issues.push(vi_issue_slabels[i])
        }
        if (issues.length > 0) {
            cds_targetinfo.data['VI_issue_flag'][ifiberslider.value] = ( issues.join('') )
        } else {
            cds_targetinfo.data['VI_issue_flag'][ifiberslider.value] = " "
        }
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        cds_targetinfo.change.emit()
        """
    vi_issue_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo,ifiberslider = ifiberslider, 
                vi_issue_input=vi_issue_input, vi_issue_labels=vi_issue_labels,
                vi_issue_slabels=vi_issue_slabels,
                title=title, vi_file_fields = vi_file_fields), 
        code=vi_issue_code )
    vi_issue_input.js_on_click(vi_issue_callback)
    
    #- Optional VI information on redshift
    vi_z_input = TextInput(value='', title="VI redshift :")
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_z_code = f.read()
    vi_z_code += """
        cds_targetinfo.data['VI_z'][ifiberslider.value]=vi_z_input.value
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        cds_targetinfo.change.emit()
        """
    vi_z_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, ifiberslider = ifiberslider, vi_z_input=vi_z_input, 
                  title=title, vi_file_fields=vi_file_fields), 
        code=vi_z_code )
    vi_z_input.js_on_change('value',vi_z_callback)
    
    #- Optional VI information on spectral type
    vi_spectypes = [" "] + utils_specviewer._vi_spectypes
    vi_category_select = Select(value=" ", title="VI spectype :", options=vi_spectypes)
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_category_code = f.read()
    vi_category_code += """
        cds_targetinfo.data['VI_spectype'][ifiberslider.value]=vi_category_select.value
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        cds_targetinfo.change.emit()
        """
    vi_category_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, ifiberslider = ifiberslider,
                  vi_category_select=vi_category_select,
                  title=title, vi_file_fields=vi_file_fields), 
        code=vi_category_code )
    vi_category_select.js_on_change('value',vi_category_callback)

    #- Optional VI comment
    vi_comment_input = TextInput(value='', title="VI comment (100 char max.) :")
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_comment_code = f.read()
    vi_comment_code += """
        cds_targetinfo.data['VI_comment'][ifiberslider.value]=vi_comment_input.value
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        cds_targetinfo.change.emit()
        """
    vi_comment_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, ifiberslider = ifiberslider, vi_comment_input=vi_comment_input, 
                  title=title, vi_file_fields=vi_file_fields), 
        code=vi_comment_code )
    vi_comment_input.js_on_change('value',vi_comment_callback)

    #- VI scanner name    
    vi_name_input = TextInput(value=(cds_targetinfo.data['VI_scanner'][0]).strip(), title="Your name :")
    with open(os.path.join(js_dir,"autosave_vi.js"), 'r') as f : vi_name_code = f.read()
    vi_name_code += """
        for (var i=0; i<nspec; i++) {
            cds_targetinfo.data['VI_scanner'][i]=vi_name_input.value
        }
        var newname = vi_filename_input.value
        var pepe = newname.split("_")
        newname = ( pepe.slice(0,pepe.length-1).join("_") ) + ("_"+vi_name_input.value+".csv")
        vi_filename_input.value = newname
        autosave_vi(title, vi_file_fields, cds_targetinfo.data)
        """
    vi_name_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, nspec = nspec, vi_name_input=vi_name_input,
                 vi_filename_input=vi_filename_input, title=title, vi_file_fields=vi_file_fields), 
        code=vi_name_code )
    vi_name_input.js_on_change('value',vi_name_callback)

    #- Guidelines for VI flags
    vi_guideline_txt = "<B> VI guidelines </B>"
    vi_guideline_txt += "<BR /> <B> Classification flags : </B>"
    for flag in utils_specviewer._vi_flags :
        if flag['type'] == 'class' : vi_guideline_txt += ("<BR />&emsp;&emsp;[&emsp;"+flag['label']+"&emsp;] "+flag['description'])
    vi_guideline_txt += "<BR /> <B> Optional indications : </B>"
    for flag in utils_specviewer._vi_flags :
        if flag['type'] == 'issue' : 
            vi_guideline_txt += ( "<BR />&emsp;&emsp;[&emsp;" + flag['label'] + 
                                 "&emsp;(" + flag['shortlabel'] + ")&emsp;] " + flag['description'] )
    vi_guideline_div = Div(text=vi_guideline_txt)

    #- Save VI info to CSV file
    save_vi_button = Button(label="Download VI", button_type="default")
    with open(os.path.join(js_dir,"FileSaver.js"), 'r') as f : save_vi_code = f.read()
    with open(os.path.join(js_dir,"download_vi.js"), 'r') as f : save_vi_code += f.read()
    save_vi_callback = CustomJS(
        args=dict(cds_targetinfo=cds_targetinfo, 
            vi_file_fields=vi_file_fields, vi_filename_input=vi_filename_input), 
        code=save_vi_code ) 
    save_vi_button.js_on_event('button_click', save_vi_callback)

    #- Recover auto-saved VI data in browser
    recover_vi_button = Button(label="Recover auto-saved VI", button_type="default")
    with open(os.path.join(js_dir,"recover_autosave_vi.js"), 'r') as f : recover_vi_code = f.read()
    recover_vi_callback = CustomJS(
        args = dict(title=title, vi_file_fields=vi_file_fields, cds_targetinfo=cds_targetinfo, 
                   ifiber=ifiberslider.value, vi_comment_input=vi_comment_input,
                   vi_name_input=vi_name_input, vi_class_input=vi_class_input, vi_issue_input=vi_issue_input,
                   vi_issue_slabels=vi_issue_slabels, vi_class_labels=vi_class_labels),
        code = recover_vi_code )
    recover_vi_button.js_on_event('button_click', recover_vi_callback)
    
    #- Clear all auto-saved VI
    clear_vi_button = Button(label="Clear all auto-saved VI", button_type="default")
    clear_vi_callback = CustomJS( args = dict(), code = """
        localStorage.clear()
        """ )
    clear_vi_button.js_on_event('button_click', clear_vi_callback)

    #- Show VI in a table
    vi_table_columns = [
        TableColumn(field="VI_class_flag", title="Flag", width=40),
        TableColumn(field="VI_issue_flag", title="Opt.", width=50),
        TableColumn(field="VI_z", title="VI z", width=50),
        TableColumn(field="VI_spectype", title="VI spectype", width=150),
        TableColumn(field="VI_comment", title="VI comment", width=200)
    ]
    vi_table = DataTable(source=cds_targetinfo, columns=vi_table_columns, width=500)
    vi_table.height = 10 * vi_table.row_height
    
#     # Choose to show or not previous VI
#     show_prev_vi_select = Select(title='Show previous VI', value='No', options=['Yes','No'])
#     show_prev_vi_callback = CustomJS(args=dict(vi_info_div = vi_info_div, show_prev_vi_select=show_prev_vi_select, targetinfo = cds_targetinfo, ifiberslider = ifiberslider), code="""
#         if (show_prev_vi_select.value == "Yes") {
#             vi_info_div.text = targetinfo.data['vi_info'][ifiberslider.value];
#         } else {
#             vi_info_div.text = " ";
#         }
#     """)
#     show_prev_vi_select.js_on_change('value',show_prev_vi_callback)


    #-----
    #- Main js code to update plot
    with open(os.path.join(js_dir,"update_plot.js"), 'r') as f : update_plot_code = f.read()
    update_plot = CustomJS(
        args = dict(
            spectra = cds_spectra,
            coaddcam_spec = cds_coaddcam_spec,
            model = cds_model,
            targetinfo = cds_targetinfo,
#            target_info_div = target_info_div,
## BYPASS DIV
            zcat_disp_cds = zcat_disp_cds,
            targ_disp_cds = targ_disp_cds,
 #           vi_info_div = vi_info_div,
 #           show_prev_vi_select = show_prev_vi_select,
            ifiberslider = ifiberslider,
            smootherslider = smootherslider,
            zslider=zslider,
            dzslider=dzslider,
            fig = fig,
            imfig_source=imfig_source,
            imfig_urls=imfig_urls,
            vi_comment_input = vi_comment_input,
            vi_name_input = vi_name_input,
            vi_class_input = vi_class_input,
            vi_class_labels = vi_class_labels,
            vi_issue_input = vi_issue_input,
            vi_z_input = vi_z_input, vi_category_select = vi_category_select,
            vi_issue_slabels = vi_issue_slabels
            ),
        code = update_plot_code
    )
    smootherslider.js_on_change('value', update_plot)
    ifiberslider.js_on_change('value', update_plot)


    #-----
    #- Bokeh setup
    # NB widget height / width are still partly hardcoded, but not arbitrary except for Spacers
    
    slider_width = plot_width - 2*navigation_button_width
    navigator = bk.Row(
        widgetbox(prev_button, width=navigation_button_width+15),
        widgetbox(next_button, width=navigation_button_width+20),
        widgetbox(ifiberslider, width=plot_width+(plot_height//2)-(60*len(vi_class_labels)+2*navigation_button_width+35))
    )
    if with_vi_widgets :
        navigator.children.insert(1, widgetbox(vi_class_input, width=60*len(vi_class_labels)) )
        vi_widget_set = bk.Column(
            widgetbox( Div(text="VI optional indications :"), width=300 ),
            bk.Row(
                bk.Column(
                    widgetbox(Spacer(height=20)),
                    widgetbox(vi_issue_input, width=150, height=100),
                ),
                bk.Column(
                    widgetbox(vi_z_input, width=150),
                    widgetbox(vi_category_select, width=150),
                )
            ),
            widgetbox(vi_comment_input, width=300),
            widgetbox(vi_name_input, width=150),
            widgetbox(vi_filename_input, width=300),
            widgetbox(save_vi_button, width=100),
            widgetbox(vi_table),        
            bk.Row(
                widgetbox(recover_vi_button, width=150),
                widgetbox(clear_vi_button, width=150)
            )
        )
    plot_widget_width = (plot_width+(plot_height//2))//2 - 40
    plot_widget_set = bk.Column(
        widgetbox( Div(text="Pipeline fit : ") ),
        widgetbox(zcat_display, width=plot_widget_width),
        bk.Row(
            widgetbox(zslider, width=plot_width//2 - 110),
            widgetbox(z_display, width=120)
        ),
        bk.Row(
            widgetbox(dzslider, width=plot_width//2 - 110),
            widgetbox(zreset_button, width=100)
        ),
        widgetbox(smootherslider, width=plot_widget_width),
        widgetbox(display_options_group,width=120),
        widgetbox(coaddcam_buttons, width=200),
        widgetbox(waveframe_buttons, width=120),
        widgetbox(lines_button_group, width=200),
        widgetbox(majorline_checkbox, width=120)
    )
    if with_vi_widgets :
        plot_widget_set.children.append( widgetbox(Spacer(height=30)) )
        plot_widget_set.children.append( widgetbox(vi_guideline_div, width=plot_widget_width) )
        full_widget_set = bk.Row(
            vi_widget_set,
            widgetbox(Spacer(width=40)),
            plot_widget_set
        )
    else : full_widget_set = plot_widget_set
    
    main_bokehsetup = bk.Column(
        bk.Row(fig, bk.Column(imfig, zoomfig), Spacer(width=20), sizing_mode='stretch_width'),
        bk.Row(
            widgetbox(targ_display, width=plot_width - 120),
            widgetbox(reset_plotrange_button, width = 120)
        ),
        navigator,
        full_widget_set,
        sizing_mode='stretch_width'
    )
    
    if with_thumb_tab is False :
        full_viewer = main_bokehsetup
    else :
        full_viewer = Tabs()
        ncols_grid = 5 # TODO un-hardcode
        titles = None # TODO define
        miniplot_width = ( plot_width + (plot_height//2) ) // ncols_grid
        thumb_grid = grid_thumbs(spectra, miniplot_width, x_range=(xmin,xmax), ncols_grid=ncols_grid, titles=titles)
        tab1 = Panel(child = main_bokehsetup, title='Main viewer')
        tab2 = Panel(child = thumb_grid, title='Gallery')
        full_viewer.tabs=[ tab1, tab2 ]
        
        # Dirty trick : callback functions on thumbs need to be defined AFTER the full_viewer is implemented
        # Otherwise, at least one issue = no toolbar anymore for main fig. (apparently due to ifiberslider in callback args)
        for i_spec in range(nspec) :
            thumb_callback = CustomJS(args=dict(full_viewer=full_viewer, i_spec=i_spec, ifiberslider=ifiberslider), code="""
            full_viewer.active = 0
             ifiberslider.value = i_spec
            """)
            (thumb_grid.children[i_spec][0]).js_on_event(bokeh.events.Tap, thumb_callback)

    if notebook:
        bk.show(full_viewer)
    else:
        bk.save(full_viewer)

    #-----
    #- "Light" Bokeh setup including only the thumbnail gallery
    if with_thumb_only_page :
        thumb_page = html_page.replace("specviewer_"+title, "thumbs_specviewer_"+title)
        bk.output_file(thumb_page, title='DESI spectral viewer - thumbnail gallery')
        ncols_grid = 5 # TODO un-hardcode
        titles = None # TODO define
        miniplot_width = ( plot_width + (plot_height//2) ) // ncols_grid
        thumb_grid = grid_thumbs(spectra, miniplot_width, x_range=(xmin,xmax), ncols_grid=ncols_grid, titles=titles)
        thumb_viewer = bk.Column(
            widgetbox( Div(text=
                           " <h3> Thumbnail gallery for DESI spectra in "+title+" </h3>" +
                           " <p> Click <a href='specviewer_"+title+".html'>here</a> to access the spectral viewer corresponding to these spectra. </p>"
                          ), width=plot_width ),
            widgetbox( thumb_grid )
        )
        bk.save(thumb_viewer)
    

#-------------------------------------------------------------------------
_line_list = [
    #
    # This is the set of emission lines from the spZline files.
    # See $IDLSPEC2D_DIR/etc/emlines.par
    # Wavelengths are in air for lambda > 2000, vacuum for lambda < 2000.
    # TODO: convert to vacuum wavelengths
    #
    {"name" : "Lyα",      "longname" : "Lyman α",        "lambda" : 1215.67,  "emission": True, "major": True  },
    {"name" : "Lyβ",      "longname" : "Lyman β",        "lambda" : 1025.18,  "emission": True, "major": False },
    {"name" : "N V",      "longname" : "N V 1240",       "lambda" : 1240.81,  "emission": True, "major": False },
    {"name" : "C IV",     "longname" : "C IV 1549",      "lambda" : 1549.48,  "emission": True, "major": True  },
    {"name" : "He II",    "longname" : "He II 1640",     "lambda" : 1640.42,  "emission": True, "major": False },
    {"name" : "C III]",   "longname" : "C III] 1908",    "lambda" : 1908.734, "emission": True, "major": False },
    {"name" : "Mg II",    "longname" : "Mg II 2799",     "lambda" : 2799.49,  "emission": True, "major": False },
    {"name" : "[O II]",   "longname" : "[O II] 3725",    "lambda" : 3726.032, "emission": True, "major": True  },
    {"name" : "[O II]",   "longname" : "[O II] 3727",    "lambda" : 3728.815, "emission": True, "major": True  },
    {"name" : "[Ne III]", "longname" : "[Ne III] 3868",  "lambda" : 3868.76,  "emission": True, "major": False },
    {"name" : "Hζ",       "longname" : "Balmer ζ",       "lambda" : 3889.049, "emission": True, "major": False },
    {"name" : "[Ne III]", "longname" : "[Ne III] 3970",  "lambda" : 3970.00,  "emission": True, "major": False },
    {"name" : "Hε",       "longname" : "Balmer ε",       "lambda" : 3970.072, "emission": True, "major": False },
    {"name" : "Hδ",       "longname" : "Balmer δ",       "lambda" : 4101.734, "emission": True, "major": False },
    {"name" : "Hγ",       "longname" : "Balmer γ",       "lambda" : 4340.464, "emission": True, "major": False },
    {"name" : "[O III]",  "longname" : "[O III] 4363",   "lambda" : 4363.209, "emission": True, "major": False },
    {"name" : "He II",    "longname" : "He II 4685",     "lambda" : 4685.68,  "emission": True, "major": False },
    {"name" : "Hβ",       "longname" : "Balmer β",       "lambda" : 4861.325, "emission": True, "major": False },
    {"name" : "[O III]",  "longname" : "[O III] 4959",   "lambda" : 4958.911, "emission": True, "major": True },
    {"name" : "[O III]",  "longname" : "[O III] 5007",   "lambda" : 5006.843, "emission": True, "major": True  },
    {"name" : "He II",    "longname" : "He II 5411",     "lambda" : 5411.52,  "emission": True, "major": False },
    {"name" : "[O I]",    "longname" : "[O I] 5577",     "lambda" : 5577.339, "emission": True, "major": False },
    {"name" : "[N II]",   "longname" : "[N II] 5755",    "lambda" : 5754.59,  "emission": True, "major": False },
    {"name" : "He I",     "longname" : "He I 5876",      "lambda" : 5875.68,  "emission": True, "major": False },
    {"name" : "[O I]",    "longname" : "[O I] 6300",     "lambda" : 6300.304, "emission": True, "major": False },
    {"name" : "[S III]",  "longname" : "[S III] 6312",   "lambda" : 6312.06,  "emission": True, "major": False },
    {"name" : "[O I]",    "longname" : "[O I] 6363",     "lambda" : 6363.776, "emission": True, "major": False },
    {"name" : "[N II]",   "longname" : "[N II] 6548",    "lambda" : 6548.05,  "emission": True, "major": False },
    {"name" : "Hα",       "longname" : "Balmer α",       "lambda" : 6562.801, "emission": True, "major": True  },
    {"name" : "[N II]",   "longname" : "[N II] 6583",    "lambda" : 6583.45,  "emission": True, "major": False },
    {"name" : "[S II]",   "longname" : "[S II] 6716",    "lambda" : 6716.44,  "emission": True, "major": False },
    {"name" : "[S II]",   "longname" : "[S II] 6730",    "lambda" : 6730.82,  "emission": True, "major": False },
    {"name" : "[Ar III]", "longname" : "[Ar III] 7135",  "lambda" : 7135.790, "emission": True, "major": False },
    #
    # Absorption lines
    #
    {"name" : "Hζ",   "longname" : "Balmer ζ",         "lambda" : 3889.049, "emission": False, "major": False },
    {"name" : "K",    "longname" : "K (Ca II 3933)",   "lambda" : 3933.7,   "emission": False, "major": False },
    {"name" : "H",    "longname" : "H (Ca II 3968)",   "lambda" : 3968.5,   "emission": False, "major": False },
    {"name" : "Hε",   "longname" : "Balmer ε",         "lambda" : 3970.072, "emission": False, "major": False },
    {"name" : "Hδ",   "longname" : "Balmer δ",         "lambda" : 4101.734, "emission": False, "major": False },
    {"name" : "G",    "longname" : "G (Ca I 4307)",    "lambda" : 4307.74,  "emission": False, "major": False },
    {"name" : "Hγ",   "longname" : "Balmer γ",         "lambda" : 4340.464, "emission": False, "major": False },
    {"name" : "Hβ",   "longname" : "Balmer β",         "lambda" : 4861.325, "emission": False, "major": False },
    {"name" : "Mg I", "longname" : "Mg I 5175",        "lambda" : 5175.0,   "emission": False, "major": False },
    {"name" : "D2",   "longname" : "D2 (Na I 5889)",   "lambda" : 5889.95,  "emission": False, "major": False },
    # {"name" : "D",    "longname" : "D (Na I doublet)", "lambda": 5892.9,   "emission": False, "major": False },
    {"name" : "D1",   "longname" : "D1 (Na I 5895)",   "lambda" : 5895.92,  "emission": False, "major": False },
    {"name" : "Hα",   "longname" : "Balmer α",         "lambda" : 6562.801, "emission": False, "major": False },
  ]

def _airtovac(w):
    """Convert air wavelengths to vacuum wavelengths. Don't convert less than 2000 Å.

    Parameters
    ----------
    w : :class:`float`
        Wavelength [Å] of the line in air.

    Returns
    -------
    :class:`float`
        Wavelength [Å] of the line in vacuum.
    """
    if w < 2000.0:
        return w;
    vac = w
    for iter in range(2):
        sigma2 = (1.0e4/vac)*(1.0e4/vac)
        fact = 1.0 + 5.792105e-2/(238.0185 - sigma2) + 1.67917e-3/(57.362 - sigma2)
        vac = w*fact
    return vac

def add_lines(fig, z=0 , emission=True, fig_height=None, label_offsets=[100, 5]):
    """
    label_offsets = [offset_absorption_lines, offset_emission_lines] : offsets in y-position 
                    for line labels wrt top (resp. bottom) of the figure
    """
    
    if fig_height is None : fig_height = fig.plot_height

    line_data = dict()
    line_data['restwave'] = np.array([_airtovac(row['lambda']) for row in _line_list])
    line_data['plotwave'] = line_data['restwave'] * (1+z)
    line_data['name'] = [row['name'] for row in _line_list]
    line_data['longname'] = [row['name'] for row in _line_list]
    line_data['plotname'] = [row['name'] for row in _line_list]
    line_data['emission'] = [row['emission'] for row in _line_list]
    line_data['major'] = [row['major'] for row in _line_list]

    y = list()
    for i in range(len(line_data['restwave'])):
        if i == 0:
            if _line_list[i]['emission']:
                y.append(fig_height - label_offsets[0])
            else:
                y.append(label_offsets[1])
        else:
            if (line_data['restwave'][i] < line_data['restwave'][i-1]+label_offsets[0]) and \
               (line_data['emission'][i] == line_data['emission'][i-1]):
                if line_data['emission'][i]:
                    y.append(y[-1] - 15)
                else:
                    y.append(y[-1] + 15)
            else:
                if line_data['emission'][i]:
                    y.append(fig_height-label_offsets[0])
                else:
                    y.append(label_offsets[1])

    line_data['y'] = y

    #- Add vertical spans to figure
    lines = list()
    labels = list()
    for w, y, name, emission in zip(
            line_data['plotwave'],
            line_data['y'],
            line_data['plotname'],
            line_data['emission']
            ):
        if emission:
            color = 'blueviolet'
        else:
            color = 'green'

        s = Span(location=w, dimension='height', line_color=color,
                line_alpha=1.0, line_dash='dashed', visible=False)

        fig.add_layout(s)
        lines.append(s)

        lb = Label(x=w, y=y, x_units='data', y_units='screen',
                    text=name, text_color='gray', text_font_size="8pt",
                    x_offset=2, y_offset=0, visible=False)
        fig.add_layout(lb)
        labels.append(lb)

    line_data = bk.ColumnDataSource(line_data)
    return line_data, lines, labels


if __name__ == '__main__':
    # framefiles = [
    #     'data/cframe-b0-00000020.fits',
    #     'data/cframe-r0-00000020.fits',
    #     'data/cframe-z0-00000020.fits',
    # ]
    #
    # frames = list()
    # for filename in framefiles:
    #     fr = desispec.io.read_frame(filename)
    #     fr = fr[0:50]  #- Trim for faster debugging
    #     frames.append(fr)
    #
    # plotspectra(frames)

    # Outdated :

    parser = argparse.ArgumentParser(description='Create html pages for the spectral viewer')
    parser.add_argument('healpixel', help='Healpixel (nside64) to process', type=str)
    parser.add_argument('--basedir', help='Path to spectra reltive to DESI_ROOT', type=str, default="datachallenge/reference_runs/18.6/spectro/redux/mini/spectra-64")
    args = parser.parse_args()
    basedir = os.environ['DESI_ROOT']+"/"+args.basedir+"/"+args.healpixel[0:2]+"/"+args.healpixel+"/"

    specfile = basedir+'spectra-64-'+args.healpixel+'.fits'
    zbfile = specfile.replace('spectra-64-', 'zbest-64-')

    #- Original remapping of individual spectra to zbest
    # spectra = desispec.io.read_spectra(specfile)
    # zbest_raw = Table.read(zbfile, 'ZBEST')

    # # EA : all is best is zbest matches spectra row-by-row.
    # zbest=Table(dtype=zbest_raw.dtype)
    # for i in range(spectra.num_spectra()) :
    #     ww, = np.where((zbest_raw['TARGETID'] == spectra.fibermap['TARGETID'][i]))
    #     if len(ww)!=1 : print("!! Issue with zbest table !!")
    #     zbest.add_row(zbest_raw[ww[0]])

    #- Coadd on the fly
    individual_spectra = desispec.io.read_spectra(specfile)
    spectra = utils_specviewer.coadd_targets(individual_spectra)
    zbest = Table.read(zbfile, 'ZBEST')

    mwave, mflux = create_model(spectra, zbest)

    ## VI "catalog" - location to define later
    vifile = os.environ['HOME']+"/prospect/vilist_prototype.fits"
    vidata = utils_specviewer.match_vi_targets(vifile, spectra.fibermap['TARGETID'])

    plotspectra(spectra, zcatalog=zbest, vidata=vidata, model=(mwave, mflux), title=os.path.basename(specfile))
