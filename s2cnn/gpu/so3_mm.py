# pylint: disable=R,C,E1101
import math
from functools import lru_cache
import torch
import s2cnn.utils.cuda as cuda_utils


class SO3_mm(torch.autograd.Function):
    def __init__(self):  # pylint: disable=W0235
        super(SO3_mm, self).__init__()

    def forward(self, x, y):  # pylint: disable=W
        self.save_for_backward(x, y)
        return so3_mm(x, y)

    def backward(self, gradz):  # pylint: disable=W
        x, y = self.saved_tensors
        nspec = x.size(0)
        nbatch = x.size(1)
        nfeature_in = x.size(2)
        nfeature_out = y.size(2)

        nl = round((3/4 * nspec)**(1/3))
        assert nspec == nl * (4 * nl**2 - 1) // 3

        gradx = grady = None

        if self.needs_input_grad[0]:
            gradx_cuda_kernel = _setup_so3mm_cuda_kernel(nl=nl, ni=nbatch, nj=nfeature_in, nk=nfeature_out, trans_y_feature=True)
            gradx = gradz.new_empty((nspec, nbatch, nfeature_in, 2))
            gradx_cuda_kernel(gradz, y, gradx)

        if self.needs_input_grad[1]:
            grady_cuda_kernel = _setup_so3mm_cuda_kernel(nl=nl, ni=nfeature_out, nj=nfeature_in, nk=nbatch, trans_out_feature=True,
                                                         conj_x=True, trans_x_spec=True, trans_x_feature=True)
            grady = gradz.new_empty((nspec, nfeature_in, nfeature_out, 2))
            grady_cuda_kernel(gradz, x, grady)

        return gradx, grady


def so3_mm(x, y):
    '''
    :param x: [l * m * n, batch,      feature_in,  complex]
    :param y: [l * m * n, feature_in, feature_out, complex]
    :return:  [l * m * n, batch,      feature_out, complex]
    '''
    assert x.is_cuda and x.dtype == torch.float32
    assert y.is_cuda and y.dtype == torch.float32
    assert y.size(3) == 2
    assert x.size(3) == 2
    nbatch = x.size(1)
    nfeature_in = x.size(2)
    nfeature_out = y.size(2)
    assert y.size(1) == nfeature_in
    nspec = x.size(0)
    assert y.size(0) == nspec
    nl = round((3/4 * nspec)**(1/3))
    assert nspec == nl * (4 * nl**2 - 1) // 3

    cuda_kernel = _setup_so3mm_cuda_kernel(nl=nl, ni=nbatch, nj=nfeature_out, nk=nfeature_in, conj_y=True, trans_y_spec=True)

    output = x.new_empty((nspec, nbatch, nfeature_out, 2))
    cuda_kernel(x, y, output)  # [l * m * n, batch, feature_out, complex]

    return output


@lru_cache(maxsize=32)
def _setup_so3mm_cuda_kernel(nl, ni, nj, nk,
                             conj_x=False, conj_y=False,
                             trans_x_spec=False, trans_x_feature=False,
                             trans_y_spec=False, trans_y_feature=False,
                             trans_out_feature=False):
    '''
    return a function that computes
        out[l*m*n, i, j] = sum_k sum_p x[l*m*p, i, k] y[l*p*n, k, j]
    where out, x, y are complex valued

    if conj_x is set to True, x is conjugated
    if conj_y is set to True, y is conjugated
    if trans_x_spec is set to True m and p are permuted in x[...]
    if trans_y_spec is set to True p and n are permuted in y[...]
    if trans_x_feature is set to True i and k are permuted in x[...]
    if trans_y_feature is set to True k and j are permuted in y[...]
    if trans_out_feature is set to True i and j are permuted in out[...]
    '''

    kernel = '''
#define NI {}
#define NJ {}
#define NK {}
'''.format(ni, nj, nk)

    if not trans_x_spec and not trans_x_feature:
        kernel += '#define INDEX_X (((L0 + m * L + p) * NI + i) * NK + k)\n'
    if not trans_x_spec and trans_x_feature:
        kernel += '#define INDEX_X (((L0 + m * L + p) * NK + k) * NI + i)\n'
    if trans_x_spec and not trans_x_feature:
        kernel += '#define INDEX_X (((L0 + p * L + m) * NI + i) * NK + k)\n'
    if trans_x_spec and trans_x_feature:
        kernel += '#define INDEX_X (((L0 + p * L + m) * NK + k) * NI + i)\n'

    if not trans_y_spec and not trans_y_feature:
        kernel += '#define INDEX_Y (((L0 + p * L + n) * NK + k) * NJ + j)\n'
    if not trans_y_spec and trans_y_feature:
        kernel += '#define INDEX_Y (((L0 + p * L + n) * NJ + j) * NK + k)\n'
    if trans_y_spec and not trans_y_feature:
        kernel += '#define INDEX_Y (((L0 + n * L + p) * NK + k) * NJ + j)\n'
    if trans_y_spec and trans_y_feature:
        kernel += '#define INDEX_Y (((L0 + n * L + p) * NJ + j) * NK + k)\n'

    if not trans_out_feature:
        kernel += '#define INDEX_OUT (((L0 + m * L + n) * NI + i) * NJ + j)\n'
    if trans_out_feature:
        kernel += '#define INDEX_OUT (((L0 + m * L + n) * NJ + j) * NI + i)\n'

    kernel += '''
#define CONJ_X {}
#define CONJ_Y {}
'''.format("x_im = -x_im;" if conj_x else ";", "y_im = -y_im;" if conj_y else ";")

    kernel += '''
#define CEIL_DIV(x, y) (((x) + (y) - 1) / (y))

extern "C"
__global__ void main_(const float* in_x, const float* in_y, float* out)
{
    // start of thread independant code
    int l = blockIdx.z;
    int L = 2 * l + 1;
    int L0 = (4 * l*l - 1) * l / 3;

    if (blockIdx.y * 32 >= L * NI || blockIdx.x * 32 >= L * NJ) {
        return;
    }

    int ntile = CEIL_DIV(L * NK, 32);
    // end of thread independant code

    int mi = blockIdx.y * 32 + threadIdx.y;
    int m = mi / NI;
    int i = mi % NI;
    int nj = blockIdx.x * 32 + threadIdx.x;
    int n = nj / NJ;
    int j = nj % NJ;

    float sum_re = 0.0;
    float sum_im = 0.0;

    for (int tile = 0; tile < ntile; ++tile) {
        __shared__ float tileX[2][32][32];
        __shared__ float tileY[2][32][32];

        int pk = tile * 32 + threadIdx.x;
        int p = pk / NK;
        int k = pk % NK;
        int index = INDEX_X * 2;
        tileX[0][threadIdx.y][threadIdx.x] = m < L && p < L ? in_x[index + 0] : 0.0;
        tileX[1][threadIdx.y][threadIdx.x] = m < L && p < L ? in_x[index + 1] : 0.0;

        pk = tile * 32 + threadIdx.y;
        p = pk / NK;
        k = pk % NK;
        index = INDEX_Y * 2;
        tileY[0][threadIdx.y][threadIdx.x] = p < L && n < L ? in_y[index + 0] : 0.0;
        tileY[1][threadIdx.y][threadIdx.x] = p < L && n < L ? in_y[index + 1] : 0.0;

        __syncthreads();

        for (int any = 0; any < 32; ++any) {
            float x_re = tileX[0][threadIdx.y][any];
            float x_im = tileX[1][threadIdx.y][any];
            float y_re = tileY[0][any][threadIdx.x];
            float y_im = tileY[1][any][threadIdx.x];

            CONJ_X
            CONJ_Y

            sum_re += x_re * y_re - x_im * y_im;
            sum_im += x_re * y_im + x_im * y_re;
        }

        __syncthreads();
    }

    if (m < L && n < L) {
        int index = INDEX_OUT * 2;
        out[index + 0] = sum_re;
        out[index + 1] = sum_im;
    }
}
'''
    kernel = cuda_utils.compile_kernel(kernel, b'so3_mm.cu', 'main_')
    stream = cuda_utils.Stream(ptr=torch.cuda.current_stream().cuda_stream)

    def fun(x, y, output):
        assert output.is_contiguous()
        kernel(block=(32, 32, 1),
               grid=(math.ceil((2 * nl - 1) * nj / 32), math.ceil((2 * nl - 1) * ni / 32), nl),
               args=[x.contiguous().data_ptr(), y.contiguous().data_ptr(), output.data_ptr()],
               stream=stream)
    return fun
