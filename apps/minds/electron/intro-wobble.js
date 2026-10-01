// The jelly rig the loading document's intro pops its letters in with. Ported
// from the splash-page sketch (mind-sketches, vague-cheetah), which took it in
// turn from the character explorations. Two ideas carry the whole thing:
//
//   1. Nothing is keyframed. Every channel is a damped spring you either HOLD
//      (move its target) or KICK (throw velocity at it), and the overshoot and
//      ring-down that read as "jelly" fall out of the physics rather than being
//      authored. So arriving is one poke, not a curve.
//
//   2. The wobble is modal. Deformation is a sum of harmonics in the angle
//      about the letter's centre -- k=2 is the ellipse (the big slow sway),
//      k=3 a lopsided three-lobed roll, k=4 the fast ripple that dies first --
//      each one its own pair of springs. That is how a real droplet oscillates,
//      which is why it reads as jelly rather than as a shape being tweened.
//
// The field is applied to the path's own Bezier control points rather than to a
// resampled outline: it is smooth, so warping the controls warps the curve, and
// at zero amplitude it is the identity -- so the letter at rest is the Figma
// vector itself and not an approximation of it.
//
// Wrapped so nothing reaches the page's global scope but the namespace below:
// shell.html loads these as classic scripts, which share one top-level lexical
// scope, so a `const` here collides with one of the same name in a sibling.

(function () {
  // Semi-implicit Euler.

  /**
   * @param {number} stiffness angular-frequency-squared, 1/s^2. Higher = snappier.
   * @param {number} ratio damping ratio: <1 rings, 1 is critical, >1 is sluggish.
   */
  function makeSpring(value, stiffness, ratio) {
    return { value, velocity: 0, target: value, stiffness, ratio };
  }

  function stepSpring(s, dt) {
    const damping = 2 * s.ratio * Math.sqrt(s.stiffness);
    s.velocity += (s.stiffness * (s.target - s.value) - damping * s.velocity) * dt;
    s.value += s.velocity * dt;
  }

  /**
   * Impulse sized so the first swing peaks near `amplitude` whatever the
   * stiffness -- an undamped spring given velocity v swings to v/w, so scaling by
   * w makes the gesture's SIZE the thing you ask for and leaves stiffness to
   * control only its SPEED. Without it, stiffening a mode silently shrinks its
   * wobble.
   */
  function kick(s, amplitude) {
    s.velocity += amplitude * Math.sqrt(s.stiffness);
  }

  /** mulberry32 -- seeded, so a wobble replays identically every time. */
  function makeRandom(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /**
   * Parse the absolute M/C/Z that Figma emits for a closed vector into subpaths of
   * `{ start, curves }`, each curve `[c1x, c1y, c2x, c2y, x, y]`.
   *
   * The number pattern has to accept exponents -- Figma writes coordinates like
   * `3.05176e-05` for values that ought to be zero, and a naive `[-\d.]+` silently
   * truncates one into `3.05176` and throws that point across the glyph.
   */
  function parsePath(d) {
    const tokens = d.match(/[MCZmcz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?/g) || [];
    const subpaths = [];
    let current = null;
    let i = 0;
    while (i < tokens.length) {
      const op = tokens[i++];
      const num = () => Number(tokens[i++]);
      if (op === 'M' || op === 'm') {
        current = { start: [num(), num()], curves: [] };
        subpaths.push(current);
      } else if (op === 'C' || op === 'c') {
        if (!current) throw new Error('path: C before M');
        current.curves.push([num(), num(), num(), num(), num(), num()]);
      } else if (op === 'Z' || op === 'z') {
        current = null;
      } else {
        throw new Error(`path: unsupported command "${op}"`);
      }
    }
    return subpaths;
  }

  /** Re-emit a parsed path, pushing every point -- anchors AND controls -- through `warp`. */
  function emitPath(subpaths, warp) {
    const out = [];
    for (const sp of subpaths) {
      const [sx, sy] = warp(sp.start[0], sp.start[1]);
      out.push(`M${sx.toFixed(3)} ${sy.toFixed(3)}`);
      for (const c of sp.curves) {
        const [ax, ay] = warp(c[0], c[1]);
        const [bx, by] = warp(c[2], c[3]);
        const [px, py] = warp(c[4], c[5]);
        out.push(
          `C${ax.toFixed(3)} ${ay.toFixed(3)} ${bx.toFixed(3)} ${by.toFixed(3)} ${px.toFixed(3)} ${py.toFixed(3)}`,
        );
      }
      out.push('Z');
    }
    return out.join('');
  }

  /** Surface modes stiffen with order the way a droplet's do: w_k^2 proportional to k(k^2-1). */
  function modeStiffness(base, k) {
    return (base * (k * (k * k - 1))) / 6;
  }

  /**
   * The deformation, as a radial strain that varies with angle:
   * `r' = r * (1 + sum a_k cos(k0) + b_k sin(k0))` about (cx, cy).
   *
   * Radial-and-multiplicative rather than displacement-along-the-normal for one
   * reason: a letter is a filled body with holes in it, and the counters of the d
   * and o have to travel with the material around them. A normal displacement
   * would push a counter's boundary outward along its OWN normal, which is inward
   * for a hole -- the counter would swell as the letter squashed. A field applied
   * to every point deforms the whole body coherently, holes included. It is also
   * exactly the identity at zero amplitude, which is what lets the letter settle
   * as the drawing.
   */
  function modalWarp(cx, cy, modes) {
    return (x, y) => {
      const dx = x - cx;
      const dy = y - cy;
      if (dx === 0 && dy === 0) return [x, y];
      const theta = Math.atan2(dy, dx);
      let amp = 0;
      for (const m of modes) {
        amp += m.cos.value * Math.cos(m.k * theta) + m.sin.value * Math.sin(m.k * theta);
      }
      // Floored so a deep press dents the letter instead of turning it inside out.
      const s = Math.max(0.25, 1 + amp);
      return [cx + dx * s, cy + dy * s];
    };
  }

  const introWobble = {
    makeSpring,
    stepSpring,
    kick,
    makeRandom,
    parsePath,
    emitPath,
    modeStiffness,
    modalWarp,
  };

  // Loaded by shell.html with a plain script tag (the renderer has no require),
  // and required by the node unit tests.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = introWobble;
  }
  if (typeof window !== 'undefined') {
    window.mindsIntroWobble = introWobble;
  }
})();
