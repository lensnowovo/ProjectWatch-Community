// React Bits ColorBends shader, adapted to the existing OGL runtime.
// Theme-aware paper rendering; see LICENSE-react-bits.md.
import { useEffect, useRef } from "react";
import { Color, Mesh, Program, Renderer, Triangle } from "ogl";

const vertex = `
attribute vec2 position;
attribute vec2 uv;
varying vec2 vUv;
void main() { vUv = uv; gl_Position = vec4(position, 0.0, 1.0); }
`;
const fragment = `
#define MAX_COLORS 8
precision highp float;
uniform vec3 uBase;
uniform vec2 uCanvas;
uniform float uTime;
uniform float uSpeed;
uniform vec2 uRot;
uniform int uColorCount;
uniform vec3 uColors[MAX_COLORS];
uniform int uTransparent;
uniform float uScale;
uniform float uFrequency;
uniform float uWarpStrength;
uniform vec2 uPointer; // in NDC [-1,1]
uniform float uMouseInfluence;
uniform float uParallax;
uniform float uNoise;
uniform int uIterations;
uniform float uIntensity;
uniform float uBandWidth;
varying vec2 vUv;

void main() {
  float t = uTime * uSpeed;
  vec2 p = vUv * 2.0 - 1.0;
  p += uPointer * uParallax * 0.1;
  vec2 rp = vec2(p.x * uRot.x - p.y * uRot.y, p.x * uRot.y + p.y * uRot.x);
  vec2 q = vec2(rp.x * (uCanvas.x / uCanvas.y), rp.y);
  q /= max(uScale, 0.0001);
  q /= 0.5 + 0.2 * dot(q, q);
  q += 0.2 * cos(t) - 7.56;
  vec2 toward = (uPointer - rp);
  q += toward * uMouseInfluence * 0.2;

    for (int j = 0; j < 5; j++) {
      if (j >= uIterations - 1) break;
      vec2 rr = sin(1.5 * (q.yx * uFrequency) + 2.0 * cos(q * uFrequency));
      q += (rr - q) * 0.15;
    }

    vec3 col = vec3(0.0);
    float a = 1.0;

    if (uColorCount > 0) {
      vec2 s = q;
      vec3 sumCol = vec3(0.0);
      float cover = 0.0;
      float weightSum = 0.0;
      for (int i = 0; i < MAX_COLORS; ++i) {
            if (i >= uColorCount) break;
            s -= 0.01;
            vec2 r = sin(1.5 * (s.yx * uFrequency) + 2.0 * cos(s * uFrequency));
            float m0 = length(r + sin(5.0 * r.y * uFrequency - 3.0 * t + float(i)) / 4.0);
            float kBelow = clamp(uWarpStrength, 0.0, 1.0);
            float kMix = pow(kBelow, 0.3); // strong response across 0..1
            float gain = 1.0 + max(uWarpStrength - 1.0, 0.0); // allow >1 to amplify displacement
            vec2 disp = (r - s) * kBelow;
            vec2 warped = s + disp * gain;
            float m1 = length(warped + sin(5.0 * warped.y * uFrequency - 3.0 * t + float(i)) / 4.0);
            float m = mix(m0, m1, kMix);
            float w = 1.0 - exp(-uBandWidth / exp(uBandWidth * m));
            sumCol += uColors[i] * w;
            weightSum += w;
            cover = max(cover, w);
      }
      col = clamp(sumCol / max(weightSum, 0.0001), 0.0, 1.0);
      a = uTransparent > 0 ? cover : 1.0;
    } else {
        vec2 s = q;
        for (int k = 0; k < 3; ++k) {
            s -= 0.01;
            vec2 r = sin(1.5 * (s.yx * uFrequency) + 2.0 * cos(s * uFrequency));
            float m0 = length(r + sin(5.0 * r.y * uFrequency - 3.0 * t + float(k)) / 4.0);
            float kBelow = clamp(uWarpStrength, 0.0, 1.0);
            float kMix = pow(kBelow, 0.3);
            float gain = 1.0 + max(uWarpStrength - 1.0, 0.0);
            vec2 disp = (r - s) * kBelow;
            vec2 warped = s + disp * gain;
            float m1 = length(warped + sin(5.0 * warped.y * uFrequency - 3.0 * t + float(k)) / 4.0);
            float m = mix(m0, m1, kMix);
            col[k] = 1.0 - exp(-uBandWidth / exp(uBandWidth * m));
        }
        a = uTransparent > 0 ? max(max(col.r, col.g), col.b) : 1.0;
    }

    col *= uIntensity;

    if (uNoise > 0.0001) {
      float n = fract(sin(dot(gl_FragCoord.xy + vec2(uTime), vec2(12.9898, 78.233))) * 43758.5453123);
      col += (n - 0.5) * uNoise;
      col = clamp(col, 0.0, 1.0);
    }

    vec3 rgb = (uTransparent > 0) ? col * a : col;
    gl_FragColor = vec4(mix(uBase, col, a * 0.75), 1.0);
}
`;

type Props = { colors: string[]; baseColor: string };
export default function ColorBends(props: Props) {
  const container = useRef<HTMLDivElement>(null);
  const current = useRef(props);
  current.current = props;
  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let renderer: Renderer;
    try { renderer = new Renderer({ alpha: false, dpr: Math.min(window.devicePixelRatio || 1, 1.5) }); }
    catch { return; }
    const gl = renderer.gl;
    const rgb = (hex: string) => { const c = new Color(hex); return [c.r, c.g, c.b]; };
    const stops = () => Array.from({ length: 8 }, (_, i) => rgb(current.current.colors[i] || "#000000"));
    const program = new Program(gl, { vertex, fragment, uniforms: {
      uBase: { value: rgb(current.current.baseColor) },
      uCanvas: { value: [1, 1] }, uTime: { value: 0 }, uSpeed: { value: .08 },
      uRot: { value: [.866, .5] }, uColorCount: { value: 3 }, uColors: { value: stops() },
      uTransparent: { value: 1 }, uScale: { value: 1 }, uFrequency: { value: 1 },
      uWarpStrength: { value: 1 }, uPointer: { value: [0, 0] },
      uMouseInfluence: { value: 0 }, uParallax: { value: 0 }, uNoise: { value: .012 },
      uIterations: { value: 1 }, uIntensity: { value: 1 }, uBandWidth: { value: 3 },
    }});
    const geometry = new Triangle(gl);
    const mesh = new Mesh(gl, { geometry, program });
    host.appendChild(gl.canvas);
    const resize = () => {
      renderer.setSize(Math.max(host.clientWidth, 1), Math.max(host.clientHeight, 1));
      program.uniforms.uCanvas.value = [gl.drawingBufferWidth, gl.drawingBufferHeight];
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host); resize();
    let frame = 0, visible = true, time = 0, previous = 0;
    const render = (now: number) => {
      frame = 0;
      if (!visible || document.hidden) { previous = 0; return; }
      if (previous) time += Math.min((now - previous) / 1000, .1);
      previous = now;
      program.uniforms.uTime.value = time;
      program.uniforms.uBase.value = rgb(current.current.baseColor);
      program.uniforms.uColors.value = stops();
      program.uniforms.uColorCount.value = Math.min(current.current.colors.length, 8);
      renderer.render({ scene: mesh });
      frame = requestAnimationFrame(render);
    };
    const sync = () => {
      if (!visible || document.hidden) { cancelAnimationFrame(frame); frame = 0; previous = 0; }
      else if (!frame) frame = requestAnimationFrame(render);
    };
    const intersection = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; sync(); });
    intersection.observe(host);
    document.addEventListener("visibilitychange", sync);
    sync();
    return () => {
      cancelAnimationFrame(frame); observer.disconnect(); intersection.disconnect();
      document.removeEventListener("visibilitychange", sync);
      geometry.remove(); program.remove();
      if (gl.canvas.parentNode === host) host.removeChild(gl.canvas);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    };
  }, []);
  return <div ref={container} className="color-bends-container" />;
}
