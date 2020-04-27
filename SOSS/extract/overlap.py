import numpy as np
from scipy.sparse import lil_matrix, csr_matrix, diags, find

# Local imports
from custom_numpy import is_sorted, first_change, arange_2d
from interpolate import SegmentedLagrangeX


class _BaseOverlap():
    """
    Base class for overlaping extraction of the form:
    A f = b
    where A is a matrix and b is an array.
    We want to solve for the array f.
    The elements of f are labelled by 'k'.
    The pixels are labeled by 'i'.
    Every pixel 'i' is covered by a set of 'k' for each order
    of diffraction.
    The classes inheriting from this class should specify the 
    methods get_w which computes the 'k' associated to each pixel 'i'.
    These depends of the type of interpolation used.
    
    Parameters
    ----------
    scidata : (N, M) array_like
        A 2-D array of real values representing the detector image.
    T_list : (N_ord) list or array of functions
        A list or array of the throughput at each order.
        The functions depend on the wavelength
    P_list : (N_ord, N, M) list or array of 2-D arrays
        A list or array of the spatial profile for each order
        on the detector. It has to have the same (N, M) as `scidata`.
    lam_list : (N_ord, N, M) list or array of 2-D arrays
        A list or array of the central wavelength position for each
        order on the detector. 
        It has to have the same (N, M) as `scidata`.
    sig : (N, M) array_like, optional
        Estimate of the error on each pixel. Default is one everywhere.
    mask : (N, M) array_like boolean, optional
        Boolean Mask of the bad pixels on the detector. 
    lam_grid : (N_k) array_like, optional but recommended:
        The grid on which f(lambda) will be projected.
        Default still has to be improved.
    d_lam : float, optional:
        Step to build a default `lam_grid`, but should be replaced
        for the needed resolution.
    tresh : float, optional:
        The pixels where the estimated transmission is less than
        this value will be masked.
    """    
    def __init__(self, scidata, T_list, P_list, lam_list, lam_grid=None,
                 c_list=None, sig=None, mask=None, tresh=1e-5):
        
        # lam_grid must be specified
        if lam_grid is None:
            raise ValueError("`lam_grid` kwarg must be specified.")
        
        # Basic parameters to save
        self.n_ord = len(T_list)
        self.N_k = len(lam_grid)
        self.tresh = tresh
        
        if sig is None:
            self.sig = np.ones_like(scidata)
        else:
            self.sig = sig.copy()
        
        # Save PSF
        self.P_list = [P.copy() for P in P_list]
        
        # Save pixel wavelength
        self.lam_list = [lam.copy() for lam in lam_list]
        
        # Set convolution to identity if not given (no effect)
        if c_list is None:
            c_list = [np.array([1.])
                      for n in range(self.n_ord)]
            
        # Save the half length of kernels of each orders
        # This is utilised to get the convolved wavelength grid
        # such that it equals to lam_grid[c_hl:-c_hl]
        self.c_hl = [(c_n.shape[-1] - 1) // 2 for c_n in c_list]
        # Idea: define a lam_grid_c (convolved) instead?
        
        # Define convolution sparse matrix
        # and save length of the convolved flux
        c, N_kc_list = [], []
        for c_n in c_list:  # For each order
            sparse_c_n = self.sparse_c(c_n)
            N_kc = sparse_c_n.shape[0]
            c.append(sparse_c_n)
            N_kc_list.append(N_kc)
        self.c_list = c
        self.N_kc_list = N_kc_list
        
        # Save wavelength grid
        self.lam_grid = lam_grid.copy()
        
        # Throughput
        # Can be a callable (function) or an array
        # with the same length as lambda grid.
        T = []
        for T_n in T_list:  # For each order
            try:  # First assume it's a function
                T.append(T_n(self.lam_grid))  # Project on grid
            except TypeError:  # Assume it's an array
                T.append(T_n)  
        self.T_list = T  # Save value
        
        # Define a global mask and masks for each orders
        self.mask, self.mask_ord = self._get_masks(mask)
        
        # Computes weights (coefficients of the linear
        # combination that express the flux of a pixel
        # given the flux projected on the wavelength grid)
        # The weights depend on the method used to interpolate
        # the flux and is encoded in the class method `_get_w()`.
        w, k = [], []
        for n in range(self.n_ord):  # For each orders
            w_n, k_n = self.get_w(n)  # Compute weigths
            w.append(w_n), k.append(k_n)
        self.w, self.k = w, k  # Save values

        # Assign other trivial attributes
        self.data = scidata.copy()
        # TODO: try setting to np.nan instead
        self.data[self.mask] = 0  
        
    def _get_masks(self, mask):
            
        # Get needed attributes 
        tresh, n_ord \
            = self.getattrs('tresh', 'n_ord')
        T_list, P_list, lam_list  \
            = self.getattrs('T_list', 'P_list', 'lam_list')
        
        # Mask according to the global troughput (spectral and spatial)
        mask_P = [P  < tresh for P in P_list]
        
        # Mask pixels not covered by the wavelength grid
        mask_lam = [self.get_mask_lam(n) for n in range(n_ord)]
        
        # Apply user's defined mask
        if mask is None:
            mask_ord = np.any([mask_P, mask_lam], axis=0)
        else:
            mask_ord = np.any([mask_P, mask_lam, [mask, mask]], axis=0)

        # Mask pixels that are masked at each orders
        global_mask = np.all(mask_ord, axis=0)
        
        # Mask if mask_P not masked but mask_lam is.
        # This means that an order is contaminated by another
        # order, but the wavelength range does not cover this part
        # of the spectrum. Thus, it cannot be accounted for.
        global_mask |= (np.any(mask_lam, axis=0) 
                      & (~np.array(mask_P)).all(axis=0))
        
        # Apply this new global mask to each orders
        mask_ord = np.any([mask_lam, global_mask[None,:,:]], axis=0)
        
        return global_mask, mask_ord
    
    def inject(self, f, **kwargs):
        
        grid = self.lam_grid
        
        # Project on wavelength grid
        f_k = f(grid)
            
        return self.rebuild(f_k, **kwargs)
    
    def rebuild(self, f_k, orders=None):
        
        ma, c  \
            = self.getattrs('mask', 'c_list')
        if orders is None:
            orders = range(self.n_ord)
            
        # Distribute the flux on detector
        out = np.zeros(ma.shape)
        for n in orders:
            # Compute `a_n` at each pixels
            a_n = self._get_a(n)
            # Apply convolution on the flux
            f_c = c[n].dot(f_k)
            # Add flux to pixels
            out[~ma] += a_n.dot(f_c)
            
        # nan invalid pixels
        out[ma] = np.nan
            
        return out
    
    def sparse_c(self, c):
        '''
        Define the sparse convolution matrix
        
        Parameters:
        
        c : ndarray, (N_kc, N_kernel) or (N_kernel) if kernel constant
        '''
        N_k = self.N_k
        c = c.T
        len_ker = len(c)
        
        if len_ker % 2 != 1:
            raise ValueError("length of the convolution kernel should be odd.")

        diag_offset = np.arange(len_ker, dtype=int)
        
        if c.ndim > 1:
            N_kc = c.shape[-1]
        else:
            N_kc = N_k - len_ker + 1
            
        return diags(c, diag_offset, shape=(N_kc,N_k), format='csr')
        
    def sparse_a(self, a, n):
        '''
        Transform `a` to a sparse matrix.
        Useful to apply convolution
        '''
        # Get parameters from object attributes
        k, N_kc = self.k[n], self.N_kc_list[n]
        
        # Number of good pixels
        N_i = len(k)
        
        # Get row index
        i_k = np.indices(k.shape)[0]
        
        # Take only well defined coefficients
        row = i_k[k>=0]
        col = k[k>=0]
        data = a[k>=0]

        return csr_matrix((data, (row, col)), shape=(N_i,N_kc))
    
    def get_jp(self, k=None):
        '''
        Get indexing to build the linear system later
        '''
        print('Pre-compute indexing')
        # Get needed attributes
        n_wv = self.N_k
        if k is None:
            k = self.k_c
        
        # Initiate
        j = np.arange(n_wv)
        j_list = []
        p_list = []
        
        for k_m in k:
            j_m, p_m = [], []
            for ij in range(k_m.shape[-1]):
                j_m_ij, p_m_ij = np.where(k_m[:,ij][None,:] == j[:,None])
                j_m.append(j_m_ij), p_m.append(p_m_ij)
            j_m, p_m = np.array(j_m), np.array(p_m)
            j_list.append(j_m), p_list.append(p_m)
        self.j_list, self.p_list = j_list, p_list
        print('Done')
        
        return j_list, p_list
    
    def _get_a(self, n, sparse=True):
        
        # Get needed attributes
        mask, sig = self.mask, self.sig
        # Order dependent attributes
        T, P, w, k  \
            = self.getattrs('T_list', 'P_list',
                            'w', 'k', n=n)
        
        # Keep only valid pixels (P and sig are still 2-D)
        P, sig = P[~mask], sig[~mask]
        # Compute a at each valid pixels
        a_n = P[:,None] * T[k] * w / sig[:,None]
        
        if sparse:
            # Convert to sparse matrix
            return self.sparse_a(a_n, n)
        else:
            return a_n
        
        
    def extract(self):
        
        # Get needed attributes
        I, sig, mask, c \
            = self.getattrs('data','sig', 'mask', 'c_list')
        N_k, n_ord = self.N_k, self.n_ord
        
        # Keep only not masked values
        I, sig = I[~mask], sig[~mask]
        
        # Build b_n
        b, k_c = [], []
        for n in range(n_ord):
            # Get sparse a
            a_n = self._get_a(n)
            # Apply convolution on the matrix a
            b_n = a_n.dot(c[n])
            # Unsparse to give as an input for _build_system
            b_n, k_c_n = unsparse(b_n)
            # Save value
            b.append(b_n), k_c.append(k_c_n)
        
        # Check if k_c changed
        try:
            k_c_old = self.k_c
            for n in range(n_ord):
                if not (k_c[n]==k_c_old[n]).all():
                    raise ValueError('k_c has changed. Not normal.')
        except AttributeError:
            self.k_c = k_c

        # Compute j and p if needed (takes time)
        try:
            j, p = self.getattrs('j_list', 'p_list')
        except AttributeError:
            j, p = self.get_jp()

        # Build system
        M, d = _build_system(I/sig, b, k_c, j, p, N_k)
        
        return M, d
    
    def extract_new(self):
        
        # Get needed attributes
        I, sig, mask, c \
            = self.getattrs('data','sig', 'mask', 'c_list')
        N_k, n_ord = self.N_k, self.n_ord
        
        # Keep only not masked values
        I, sig = I[~mask], sig[~mask]
        
        # Build b_n
        b_matrix = csr_matrix((len(I), N_k))
        for n in range(n_ord):
            # Get sparse a
            a_n = self._get_a(n)
            # Apply convolution on the matrix a
            b_n = a_n.dot(c[n])
            # Add 
            b_matrix += b_n

        # Build system 
        # (B_T * B) * f = (I/sig)_T * B
        # |   M   | * f = |     d      |
        M = b_matrix.T.dot(b_matrix)
        d = csr_matrix((I/sig).T).dot(b_matrix)
        
        return M, d
    
    def getattrs(self, *args, n=None):
        
        if n is None:
            out = [getattr(self, arg) for arg in args]
        else:
            out = [getattr(self, arg)[n] for arg in args]

        if len(out) > 1:
            return out
        else:
            return out[0]
        
        
class LagrangeOverlap(_BaseOverlap):
    
    def __init__(self, *args, lagrange_ord=1, lam_grid=None, **kwargs):
        
        # Attribute specific to the interpolation method
        self.lagrange_ord = lagrange_ord
        
        # TODO: Set a default lam_grid
        
        super().__init__(*args, lam_grid=lam_grid, **kwargs)
        
    def get_mask_lam(self, n):
        
        lam, c_hl   \
            = self.getattrs('lam_list', 'c_hl', n=n)
        lam_min = self.lam_grid[c_hl]
        lam_max = self.lam_grid[-1-c_hl]
        
        mask = (lam < lam_min) | (lam > lam_max)
        
        return mask
    
    def get_w(self, n):
        
        # Get needed attributes
        order = self.lagrange_ord
        
        # Get needed attributes
        grid, mask, order \
            = self.getattrs('lam_grid', 'mask', 'lagrange_ord')
        # ... diffraction-order dependent attributes
        lam, mask_ord, c_hl  \
            = self.getattrs('lam_list', 'mask_ord', 'c_hl', n=n)
        
        # Use the convolved grid (depends on the order)
        grid = grid[c_hl:len(grid)-c_hl]
        
        # Compute delta lamda of each pixel
        # delta_lambda = lambda_plus - lambda_minus
        d_lam = - np.diff(_get_lam_p_or_m(lam), axis=0).squeeze()
        
        # Compute only for valid pixels
        lam, d_lam = lam[~mask], d_lam[~mask]
        ma = mask_ord[~mask]
        
        # Use a pre-defined interpolator
        interp = SegmentedLagrangeX(grid, order)
        
        # Get w and k
        # Init w and k
        n_i = (~mask).sum()  # Number of good pixels
        w = np.ones((order+1, n_i)) * np.nan
        k = np.ones((order+1, n_i), dtype=int) * -1
        # Compute values in grid range
        w[:,~ma] = interp.get_coeffs(lam[~ma])
        i_segment = interp.get_index(lam[~ma])
        k[:,~ma] = interp.index[:,i_segment]
        
        # Include delta lambda in the weights
        w[:,~ma] = w[:,~ma] * d_lam[~ma]
        
        return w.T, k.T


class TrpzOverlap(_BaseOverlap):
    ''' Version oversampled with trapezoidal integration '''
    
    def __init__(self, scidata, T_list,
                 P_list, lam_list, **kwargs):
        
        # Get wavelength at the boundary of each pixel
        # TODO? Could also be an input??
        lam_p, lam_m = [], []
        for lam in lam_list:  # For each order
            lp, lm = _get_lam_p_or_m(lam)  # Lambda plus or minus
            lam_p.append(lp), lam_m.append(lm) 
        self.lam_p, self.lam_m = lam_p, lam_m  # Save values
        
        # Init upper class
        super().__init__(scidata, T_list, P_list,
                         lam_list, **kwargs)
             
    def _get_LH(self, grid, n):
        """
        Find the lowest (L) and highest (H) index 
        of lam_grid for each pixels and orders.
        """
        print('Compute LH')
        
        # Get needed attributes
        mask = self.mask
        # ... order dependent attributes
        lam_p, lam_m, mask_ord  \
            = self.getattrs('lam_p', 'lam_m', 'mask_ord', n=n)
        
        # Compute only for valid pixels
        lam_p = lam_p[~mask]
        lam_m = lam_m[~mask]
        
        # Find lower (L) index in the pixel
        #
        L = np.ones(lam_m.shape, dtype=int) * -1
        cond = lam_m[:,None] <= grid[None,:]
        ind, L_good = first_change(cond, axis=-1)
        L[ind] = L_good + 1
        # Special treatment when L==grid[0], so cond is all True
        ind = np.where(cond.all(axis=-1))
        L[ind] = 0

        # Find higher (H) index in the pixel
        #
        H = np.ones(lam_p.shape, dtype=int) * -2
        cond = lam_p[:,None] >= grid[None,:]
        ind, H_good = first_change(cond, axis=-1)
        H[ind] = H_good
        # Special treatment when H==grid[-1], so cond all True
        ind = np.where(cond.all(axis=-1))
        H[ind] = len(grid) - 1

        # Set invalid pixels for this order to L=-1 and H=-2
        ma = mask_ord[~mask]
        L[ma], H[ma] = -1, -2
        
        print('Done')

        return L, H
    
    def get_mask_lam(self, n):
        
        lam_p, lam_m, c_hl   \
            = self.getattrs('lam_p', 'lam_m', 'c_hl', n=n)
        lam_min = self.lam_grid[c_hl]
        lam_max = self.lam_grid[-1-c_hl]
        
        mask = (lam_m < lam_min) | (lam_p > lam_max)
        
        return mask
    
    def get_w(self, n):
        
        print('Compute weigths and k')
        
        # Get needed attributes
        grid, mask  \
            = self.getattrs('lam_grid','mask')
        # ... order dependent attributes
        lam_p, lam_m, mask_ord, c_hl  \
            = self.getattrs('lam_p', 'lam_m',
                            'mask_ord', 'c_hl', n=n)
        
        # Use the convolved grid (depends on the order)
        grid = grid[c_hl:len(grid)-c_hl]
        
        # Compute the wavelength coverage of the grid
        d_grid = np.diff(grid)
        
        # Get LH
        L, H = self._get_LH(grid, n)  # Get indexes
        
        # Compute only valid pixels
        lam_p, lam_m = lam_p[~mask], lam_m[~mask]
        ma = mask_ord[~mask]

        # Number of used pixels
        N_i = len(L)
        i = np.arange(N_i)
        
        print('Compute k')
        
        # Define fisrt and last index of lam_grid
        # for each pixel
        k_first, k_last = -1*np.ones(N_i), -1*np.ones(N_i)
        # If lowest value close enough to the exact grid value,
        cond = (grid[L]-lam_m)/d_grid[L] <= 1.0e-8
        # special case (no need for L_i - 1)
        k_first[cond & ~ma] = L[cond & ~ma]
        lam_m[cond & ~ma] = grid[L[cond & ~ma]]
        # else, need L_i - 1
        k_first[~cond & ~ma] = L[~cond & ~ma] - 1
        # Same situation for highest value,
        cond = (lam_p-grid[H])/d_grid[H-1] <= 1.0e-8
        # special case (no need for H_i - 1)
        k_last[cond & ~ma] = H[cond & ~ma]
        lam_p[cond & ~ma] = grid[H[cond & ~ma]]
        # else, need H_i + 1
        k_last[~cond & ~ma] = H[~cond & ~ma] + 1
        
        # Generate array of all k_i. Set to -1 if not valid
        k, bad = arange_2d(k_first, k_last+1, dtype=int, return_mask=True)
        k[bad] = -1
        # Number of valid k per pixel
        N_k = np.sum(~bad, axis=-1)
        
        # Compute array of all w_i. Set to np.nan if not valid
        # Initialize
        w = np.zeros(k.shape, dtype=float)
        ####################
        ####################
        # 4 different cases
        ####################
        ####################
        
        print('compute w')

        # Valid for every cases
        w[:,0] = grid[k[:,1]] - lam_m
        w[i,N_k-1] = lam_p - grid[k[i,N_k-2]]
        
        ##################
        # Case 1, N_k == 2
        ##################
        case = (N_k == 2) & ~ma
        if case.any():
            print('N_k = 2')
            # if k_i[0] != L_i
            cond = case & (k[:,0] != L)
            w[cond,1] += lam_m[cond] - grid[k[cond,0]]
            # if k_i[-1] != H_i
            cond = case & (k[:,1] != H)
            w[cond,0] += grid[k[cond,1]] - lam_p[cond]
            # Finally
            w[case,:] *= ((lam_p[case] - lam_m[case]) / d_grid[k[case,0]])[:,None]

        ##################
        # Case 2, N_k >= 3
        ##################
        case = (N_k >= 3) & ~ma
        if case.any():
            print('N_k = 3')
            N_ki = N_k[case]
            w[case,1] = grid[k[case,1]] - lam_m[case]
            w[case,N_ki-2] += lam_p[case] - grid[k[case,N_ki-2]]
            # if k_i[0] != L_i
            cond = case & (k[:,0] != L)
            w[cond,0] *= (grid[k[cond,1]] - lam_m[cond]) / d_grid[k[cond,0]]
            w[cond,1] += (grid[k[cond,1]]-lam_m[cond]) * (lam_m[cond]-grid[k[cond,0]])  \
                                      / d_grid[k[cond,0]]
            # if k_i[-1] != H_i
            cond = case & (k[i,N_k-1] != H)
            N_ki = N_k[cond]
            w[cond,N_ki-1] *= (lam_p[cond] - grid[k[cond,N_ki-2]]) / d_grid[k[cond,N_ki-2]]
            w[cond,N_ki-2] += (grid[k[cond,N_ki-1]]-lam_p[cond])  \
                              * (lam_p[cond]-grid[k[cond,N_ki-2]])  \
                              / d_grid[k[cond,N_ki-2]]

        ##################
        # Case 3, N_k >= 4
        ##################
        case = (N_k >= 4) & ~ma
        if case.any():
            print('N_k = 4')
            N_ki = N_k[case]
            w[case,1] += grid[k[case,2]] - grid[k[case,1]]
            w[case,N_ki-2] += grid[k[case,N_ki-2]] - grid[k[case,N_ki-3]]            

        ##################
        # Case 4, N_k > 4
        ##################
        case = (N_k > 4) & ~ma
        if case.any():
            print('N_k > 4')
            i_k = np.indices(k.shape)[-1]
            cond = case[:,None] & (2 <= i_k) & (i_k < N_k[:,None]-2)
            ind1, ind2 = np.where(cond)
            w[ind1,ind2] = d_grid[k[ind1,ind2]-1] + d_grid[k[ind1,ind2]]

        
        # Finally, divide w by 2
        w /= 2.
        
        # Make sure invalid values are masked
        w[k<0] = np.nan

        print('Done')
        return w, k
        

def _get_lam_p_or_m(lam):
    '''
    Compute lambda_plus and lambda_minus
    '''
    
    lam_r = np.zeros_like(lam)
    lam_l = np.zeros_like(lam)
    
    # Def delta lambda
    d_lam = np.diff(lam, axis=1)
    
    # Define lambda left and lambda right of each pixels
    lam_r[:,:-1] = lam[:,:-1] + d_lam/2
    lam_r[:,-1] = lam[:,-1] + d_lam[:,-1]/2
    lam_l[:,1:] = lam[:,:-1] + d_lam/2  # Same values as lam_r
    lam_l[:,0] = lam[:,0] - d_lam[:,0]/2
    
    if (lam_r >= lam_l).all():
        return lam_r, lam_l
    elif (lam_r <= lam_l).all():
        return lam_l, lam_r
    else:
        raise ValueError('Bad pixel values for wavelength')



def _build_system_slow(I, b, k, n_lam):

    # Initialize
    d = np.zeros(n_lam)
    M = np.zeros((n_lam,n_lam))

    # Iterate over matrix lines (lam)
    for j in range(n_lam):
#         print(j)
        for k_n, b_n in zip(k, b):
            for i, (k_in, b_in) in enumerate(zip(k_n, b_n)):
#                 p_in = (k_in == j) & (k_in >= 0)
#                 print(k_in)
                p_in = (k_in == j)
                if p_in.any():
#                     print(I[p_in] * b_in[i])
                    d[j] += (I[i] * b_in[p_in])
                
                    for k_m, b_m in zip(k, b):
                        k_im, b_im = k_m[i], b_m[i]
                        p_im = k_im >= 0
                        np.add.at(M, (j, k_im[p_im]), b_in[p_in] * b_im[p_im])

    return M, d


def _build_system(I, b, k, j, p, n_lam):
    
    # Initialize
    d = np.zeros(n_lam)
    M = np.zeros((n_lam,n_lam))
    
    # Transpose k, j and p
    k = [k_n.T for k_n in k]
    b = [b_n.T for b_n in b]

    n_ord = len(k)
    for n in range(n_ord):

        n_ij = len(j[n])
        for ij in range(n_ij):
            np.add.at(d, j[n][ij], (I * b[n][ij])[p[n][ij]])
        
        for m in range(n_ord):
            m_ij = len(j[m])
            for ij1 in range(n_ij):
                for ij2 in range(m_ij):
                    
                    k_ij = k[m][ij2][p[n][ij1]]
                    good = k_ij >= 0 
                    if good.any():
                        np.add.at(M, (j[n][ij1][good], k_ij[good]),
                                  (b[m][ij2][p[n][ij1]] * b[n][ij1][p[n][ij1]])[good])
                    

    return M, d

def slice_4_diag(offset):
    '''
    If offset is positive, take [-offset:]
    and if negative, take [:-offset]
    '''
    
    if offset <= 0:
        return slice(-offset, None)
    else:
        return slice(None, -offset)
    
def unsparse(matrix, fill_value=np.nan):
    
    col, row, val = find(matrix.T)
    N_row, N_col = matrix.shape

    good_rows, counts = np.unique(row, return_counts=True)

    # Define the new position in columns
    i_col = np.indices((N_row, counts.max()))[1]
    i_col = i_col[good_rows]
    i_col = i_col[i_col < counts[:,None]]
    
    # Create outputs and assign values
    col_out = np.ones((N_row, counts.max()), dtype=int) * -1
    col_out[row, i_col] = col
    out = np.ones((N_row, counts.max())) * fill_value
    out[row, i_col] = val
    
    return out, col_out
