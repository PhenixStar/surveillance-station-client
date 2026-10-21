# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Fisheye dewarp for Live View.

A fisheye camera delivers its whole hemisphere as one circle. This turns
it back into something readable, on the GPU, after mpv has decoded and
scaled the frame: MpvGLArea renders mpv into an offscreen texture at the
video's own size, then draws the widget through a fragment shader that
maps every output pixel to a direction and that direction to a point on
the circle (equidistant lens, r = radius * theta / (lens_fov / 2)).

Two views, both for a lens looking straight down (ceiling mount):

- panorama: the ring unrolled into two stacked 180 degree strips, the
  rim (horizon) at the top of each, so people stand upright and the
  tile keeps the camera's own shape;
- ptz: a virtual pan/tilt/zoom camera, steered with the same drag and
  scroll that otherwise pan and zoom the flat picture.

mpv's own user shaders can't do this: their runtime parameters
(//!PARAM, glsl-shader-opts) exist only in the gpu-next renderer, and
vo=libmpv renders with gpu, so a pan would mean recompiling the shader on
every drag step.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import math
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MODE_OFF = ""
MODE_PANORAMA = "panorama"
MODE_PTZ = "ptz"
MODES = {
    MODE_OFF: "Off",
    MODE_PANORAMA: "Panorama (two 180° strips)",
    MODE_PTZ: "Virtual PTZ (drag to look around, scroll to zoom)",
}

# Virtual PTZ limits and starting view (degrees).
PTZ_FOV_MIN = 20.0
PTZ_FOV_MAX = 150.0
PTZ_DEFAULT_TILT = 45.0
PTZ_DEFAULT_FOV = 80.0
# How far in from the rim the panorama strips reach (angle from the
# optical axis at their bottom edge): the middle of the circle is the
# floor right under the camera, stretched beyond use in a strip.
PANORAMA_THETA_MIN = 15.0


@dataclass(frozen=True)
class Lens:
    """Where the image circle sits in the frame and how wide the lens is.

    center_x/center_y are fractions of the frame width/height, radius a
    fraction of the frame height, fov the lens's full field of view in
    degrees. The defaults fit a full-circle 180 degree fisheye centred in
    the frame with a thin black margin.
    """

    center_x: float = 0.5
    center_y: float = 0.5
    radius: float = 0.48
    fov: float = 180.0


def parse_lens(text: str) -> Lens:
    """Lens from "center_x,center_y,radius,fov"; missing or bad fields keep
    their defaults, so a hand-edited config.toml can't break a slot."""
    default = Lens()
    values = [default.center_x, default.center_y, default.radius, default.fov]
    for i, part in enumerate(text.split(",")[:4]):
        with contextlib.suppress(ValueError):
            values[i] = float(part)
    cx, cy, radius, fov = values
    if not (0.0 < radius <= 2.0):
        radius = default.radius
    if not (10.0 <= fov <= 360.0):
        fov = default.fov
    return Lens(cx, cy, radius, fov)


@dataclass
class DewarpView:
    """The live view state of one slot's dewarp, in degrees."""

    mode: str = MODE_OFF
    lens: Lens = field(default_factory=Lens)
    pan: float = 0.0
    tilt: float = PTZ_DEFAULT_TILT
    fov: float = PTZ_DEFAULT_FOV

    @property
    def active(self) -> bool:
        return self.mode in (MODE_PANORAMA, MODE_PTZ)

    def reset(self) -> None:
        self.pan = 0.0
        self.tilt = PTZ_DEFAULT_TILT
        self.fov = PTZ_DEFAULT_FOV

    def zoom(self, delta: float) -> None:
        """Scroll: *delta* in mpv's log2 zoom units, positive zooms in."""
        if self.mode == MODE_PTZ:
            self.fov = min(PTZ_FOV_MAX, max(PTZ_FOV_MIN, self.fov * 2.0 ** (-delta)))

    def drag(self, dx: float, dy: float) -> None:
        """Drag by fractions of the widget size, content following the
        pointer like the flat picture's grab-and-drag."""
        if self.mode == MODE_PTZ:
            self.pan = (self.pan - dx * self.fov) % 360.0
            self.tilt = min(self.lens.fov / 2.0, max(0.0, self.tilt + dy * self.fov))
        elif self.mode == MODE_PANORAMA:
            self.pan = (self.pan - dx * 180.0) % 360.0


_VERTEX = """
out vec2 v_uv;
void main() {
    // One triangle covering the viewport, no vertex buffer needed.
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_FRAGMENT = """
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
uniform int u_mode;           // 1 panorama, 2 ptz
uniform vec2 u_center;        // circle centre, fractions of width/height
uniform float u_radius;       // circle radius, fraction of height
uniform float u_src_aspect;   // source width / height
uniform float u_half_fov;     // lens half field of view (radians)
uniform float u_pan;          // radians
uniform float u_tilt;         // radians, 0 = straight down the axis
uniform float u_fov;          // ptz field of view (radians)
uniform float u_out_aspect;   // output width / height
uniform float u_theta_min;    // panorama strip bottom (radians)
const float PI = 3.14159265358979;

vec4 sample_dir(vec3 d) {
    d = normalize(d);
    float theta = acos(clamp(d.z, -1.0, 1.0));
    if (theta > u_half_fov) return vec4(0.0, 0.0, 0.0, 1.0);
    float phi = atan(d.y, d.x);
    float rr = u_radius * theta / u_half_fov;
    vec2 src = u_center + vec2(rr * cos(phi) / u_src_aspect, rr * sin(phi));
    if (src.x < 0.0 || src.x > 1.0 || src.y < 0.0 || src.y > 1.0) return vec4(0.0, 0.0, 0.0, 1.0);
    // The texture holds the frame top at t = 1 (rendered with flip_y).
    return texture(u_tex, vec2(src.x, 1.0 - src.y));
}

void main() {
    vec2 o = vec2(v_uv.x, 1.0 - v_uv.y);  // output position, y down
    vec3 d;
    if (u_mode == 2) {
        float t = tan(u_fov * 0.5);
        vec3 r = vec3((o.x * 2.0 - 1.0) * t * u_out_aspect, (o.y * 2.0 - 1.0) * t, 1.0);
        float ct = cos(u_tilt), st = sin(u_tilt);
        r = vec3(r.x, r.y * ct - r.z * st, r.y * st + r.z * ct);
        float cp = cos(u_pan), sp = sin(u_pan);
        d = vec3(r.x * cp - r.y * sp, r.x * sp + r.y * cp, r.z);
    } else {
        float lower = o.y >= 0.5 ? 1.0 : 0.0;
        float ly = (o.y - 0.5 * lower) * 2.0;
        float phi = (o.x + lower) * PI + u_pan;
        float theta = u_half_fov - ly * (u_half_fov - u_theta_min);
        d = vec3(sin(theta) * cos(phi), sin(theta) * sin(phi), cos(theta));
    }
    frag = sample_dir(d);
}
"""


def shader_sources(use_es: bool) -> tuple[str, str]:
    """(vertex, fragment) for the GL flavour GTK gave the widget."""
    header = "#version 300 es\nprecision highp float;\n" if use_es else "#version 330 core\n"
    return header + _VERTEX, header + _FRAGMENT


class FisheyeRenderer:
    """GL objects for one widget's dewarp; create and use only with that
    widget's GL context current."""

    def __init__(self, use_es: bool) -> None:
        from OpenGL import GL

        self._gl = GL
        vertex, fragment = shader_sources(use_es)
        self._program = self._link(vertex, fragment)
        self._vao = GL.glGenVertexArrays(1)
        self._fbo = 0
        self._tex = 0
        self._size = (0, 0)
        self._uniforms = {
            name: GL.glGetUniformLocation(self._program, name)
            for name in (
                "u_tex",
                "u_mode",
                "u_center",
                "u_radius",
                "u_src_aspect",
                "u_half_fov",
                "u_pan",
                "u_tilt",
                "u_fov",
                "u_out_aspect",
                "u_theta_min",
            )
        }

    def _link(self, vertex: str, fragment: str) -> int:
        gl = self._gl
        program = gl.glCreateProgram()
        shaders = []
        for kind, source in ((gl.GL_VERTEX_SHADER, vertex), (gl.GL_FRAGMENT_SHADER, fragment)):
            shader = gl.glCreateShader(kind)
            gl.glShaderSource(shader, source)
            gl.glCompileShader(shader)
            if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                raise RuntimeError(f"fisheye shader: {gl.glGetShaderInfoLog(shader)!r}")
            gl.glAttachShader(program, shader)
            shaders.append(shader)
        gl.glLinkProgram(program)
        for shader in shaders:
            gl.glDeleteShader(shader)
        if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
            raise RuntimeError(f"fisheye program: {gl.glGetProgramInfoLog(program)!r}")
        return int(program)

    def target(self, width: int, height: int) -> int:
        """The offscreen framebuffer mpv renders the frame into, (re)sized
        to *width* x *height*."""
        gl = self._gl
        if self._fbo and self._size == (width, height):
            return self._fbo
        if not self._fbo:
            self._fbo = int(gl.glGenFramebuffers(1))
            self._tex = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
        gl.glTexImage2D(
            gl.GL_TEXTURE_2D,
            0,
            gl.GL_RGBA8,
            width,
            height,
            0,
            gl.GL_RGBA,
            gl.GL_UNSIGNED_BYTE,
            ctypes.c_void_p(0),
        )
        for param, value in (
            (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
            (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
            (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
            (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
        ):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._fbo)
        gl.glFramebufferTexture2D(
            gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, self._tex, 0
        )
        self._size = (width, height)
        return self._fbo

    def draw(self, fbo: int, width: int, height: int, view: DewarpView) -> None:
        """Draw the dewarped frame into *fbo* (the widget's), *width* x
        *height* pixels."""
        gl = self._gl
        src_w, src_h = self._size
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glViewport(0, 0, width, height)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(self._program)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex)
        u = self._uniforms
        lens = view.lens
        gl.glUniform1i(u["u_tex"], 0)
        gl.glUniform1i(u["u_mode"], 2 if view.mode == MODE_PTZ else 1)
        gl.glUniform2f(u["u_center"], lens.center_x, lens.center_y)
        gl.glUniform1f(u["u_radius"], lens.radius)
        gl.glUniform1f(u["u_src_aspect"], src_w / src_h if src_h else 1.0)
        gl.glUniform1f(u["u_half_fov"], math.radians(lens.fov / 2.0))
        gl.glUniform1f(u["u_pan"], math.radians(view.pan))
        gl.glUniform1f(u["u_tilt"], math.radians(view.tilt))
        gl.glUniform1f(u["u_fov"], math.radians(view.fov))
        gl.glUniform1f(u["u_out_aspect"], width / height if height else 1.0)
        gl.glUniform1f(u["u_theta_min"], math.radians(PANORAMA_THETA_MIN))
        gl.glBindVertexArray(self._vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def free(self) -> None:
        gl = self._gl
        with contextlib.suppress(Exception):
            if self._fbo:
                gl.glDeleteFramebuffers(1, [self._fbo])
                gl.glDeleteTextures([self._tex])
            gl.glDeleteVertexArrays(1, [self._vao])
            gl.glDeleteProgram(self._program)
        self._fbo = self._tex = 0
