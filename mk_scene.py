import numpy as np

def mk_disk_images(box_width = 79, rdisk = 7.5, wdisk = 3.0, tilt = 45.0, 
                   sky_angle = 45.0, star_to_disk_ratio = 1.0):

    # create a scene with a central star and a disk. The disk has some 
    # forward scattering an can be tilted in sky plane and line-of-sight angle
    #
    # box_width : size of the box. Needs to be odd; the star will be added on 
    #             the median pixel
    #
    # rdisk : radius of disk in pixels. Can be fractional
    # wdisk : e-width of the disk in pixels.
    #
    # tilt : line-of-sight tilt of the disk. Tilt = 0 would make the disk 
    #         edge-on, tilt = 70 makes it nearly-but-not-quite face-on
    #
    # sky_angle : rotate the position angle of the disk
    #    
    # star_to_disk_ratio : star is brighter than disk by this factor
    
    # create the box
    im = np.zeros([box_width,box_width])
    
    # create indices that define disk position
    x1,y1 = np.indices([box_width,box_width],dtype = float)-box_width//2
    # rotate and tilt coordinates from disk to sky
    x2 = np.cos(sky_angle/(180/np.pi))*x1 + np.sin(sky_angle/(180/np.pi))*y1
    y2 = -np.sin(sky_angle/(180/np.pi))*x1 + np.cos(sky_angle/(180/np.pi))*y1
    x2 = x2/np.sin(tilt/(180/np.pi))
    
    # radius in disk coordinates
    r = np.sqrt( x2**2+y2**2 ) 
    
    # forward scattering function
    scatter = np.sin(np.arctan2(x2,y2))*np.cos(tilt/(180/np.pi))+1
    
    # flux of disk
    disk = np.exp(-.5*(r-rdisk)**2/wdisk*2)*scatter
    
    # we add a bright star in the middle
    im[box_width//2,box_width//2] = np.sum(disk)*star_to_disk_ratio
    
    # adding disk to star
    im = im+disk
    
    # normalize image to unity
    im /= np.sum(im)

    return im