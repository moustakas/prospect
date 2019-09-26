"""
prospect.scripts.prepare_htmlfiles
===================================

Write html index pages from existing pages/images produced by plotframes
"""


import os, glob, stat
import argparse
from desiutil.log import get_logger

from jinja2 import Environment, FileSystemLoader


def parse() :

    parser = argparse.ArgumentParser(description="Write html index pages")
    parser.add_argument('--webdir', help='Base directory for webpages', type=str, default=None)
    parser.add_argument('--template_dir', help='Template directory', type=str, default=None)
    parser.add_argument('--pixels', help='Pixel-based arborescence', action='store_true')
    parser.add_argument('--nights', help='Night-based arborescence', action='store_true')
    args = parser.parse_args()
    return args

def main(args) :

    log = get_logger()

    webdir = args.webdir
    if webdir is None : webdir = os.environ["DESI_WWW"]+"/users/armengau/svdc2019c" # TMP, for test
    template_dir = args.template_dir
    if template_dir is None : template_dir="../templates" # TMP, to define better.

    env = Environment(loader=FileSystemLoader(template_dir))
    template_index = env.get_template('template_index.html')
    template_expolist = env.get_template('template_expo_list.html')
    template_pixellist = env.get_template('template_pixel_list.html')
    template_vignettelist = env.get_template('template_vignettelist.html')

    if args.nights :
        # Could re-write to avoid duplicates
        # TO EDIT (old expo-based code) once nights working and ok
  ### Exposure-based : for each expo create an index of fiber subsets, and a vignette webpage for each fiber subset 
## Arborescence : webdir/exposures/expoN/ =>  specviewer_expoN_fibersetM.html ; vignettes/*.png
        exposures=os.listdir(webdir+"/exposures")
        for expo in exposures :
            basedir = webdir+"/exposures/"+expo
            pp=glob.glob(basedir+"/specviewer_"+expo+"_*.html")
            subsets = [int(x[x.find("fiberset")+8:-5]) for x in pp]
            subsets.sort()
            subsets = [str(x) for x in subsets]
            pp=glob.glob(basedir+"/vignettes/*.png")
            nspec = len(pp)
            pagetext=template_expolist.render(expo=expo, fiberlist=subsets, nspec=nspec)
            with open(basedir+"/index_"+expo+".html", "w") as fh:
                fh.write(pagetext)
                fh.close()
            for fiber in subsets :
                pp = glob.glob(basedir+"/vignettes/"+expo+"_fiberset"+fiber+"_*.png")
                vignettelist = [os.path.basename(x) for x in pp]
                pagetext = template_vignettelist.render(set=expo, i_subset=fiber, n_subsets=len(subsets), imglist=vignettelist)
                with open(basedir+"/vignettelist_"+expo+"_"+fiber+".html", "w") as fh:
                    fh.write(pagetext)
                    fh.close()
            for thedir in [basedir,basedir+"/vignettes"] : os.system("chmod a+rx "+thedir)
            for x in glob.glob(basedir+"/*.html") : os.system("chmod a+r "+x)
            for x in glob.glob(basedir+"/vignettes/*.png") : os.system("chmod a+r "+x)

    if args.pixels :
    
        pixels = os.listdir( os.path.join(webdir,"pixels") )
        for pix in pixels :
            pixel_dir = os.path.join(webdir,"pixels",pix)
            spec_pages = glob.glob( pixel_dir+"/specviewer_"+pix+"_*.html" )
            subsets = [ x[len(pixel_dir+"/specviewer_"+pix)+1:-5] for x in spec_pages ]
            subsets.sort(key=int)
            img_list = glob.glob( pixel_dir+"/vignettes/*.png" )
            nspec = len(img_list)
            pagetext = template_pixellist.render(pixel=pix, subsets=subsets, nspec=nspec)
            with open( os.path.join(pixel_dir,"index_"+pix+".html"), "w") as fh:
                fh.write(pagetext)
                fh.close()
            for subset in subsets :
                img_sublist = [ os.path.basename(x) for x in img_list if pix+"_"+subset in x ]
                pagetext = template_vignettelist.render(set=pix, i_subset=subset, n_subsets=len(subsets), imglist=img_sublist)
                with open( os.path.join(pixel_dir,"vignettelist_"+pix+"_"+subset+".html"), "w") as fh:
                    fh.write(pagetext)
                    fh.close()
            for thedir in [pixel_dir, os.path.join(pixel_dir,"vignettes") ] : 
                st = os.stat(thedir)
                os.chmod(thedir, st.st_mode | stat.S_IROTH | stat.S_IXOTH) # "chmod a+rx "
            for x in glob.glob(pixel_dir+"/*.html") : 
                st = os.stat(x)
                os.chmod(x, st.st_mode | stat.S_IROTH) # "chmod a+r "
            for x in glob.glob(pixel_dir+"/vignettes/*.png") : 
                st = os.stat(x)
                os.chmod(x, st.st_mode | stat.S_IROTH) # "chmod a+r "
            log.info("pixel done : "+pix)

        pagetext = template_index.render(pixels=pixels, exposures=[""]) # TODO complete exposures 
        indexfile = os.path.join(webdir,"index.html")
        with open(indexfile, "w") as fh:
            fh.write(pagetext)
            fh.close()
            st = os.stat(indexfile)
            os.chmod(indexfile, st.st_mode | stat.S_IROTH) # "chmod a+r"
