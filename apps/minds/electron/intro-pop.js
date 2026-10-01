// The intro's arrival: the six letters of the wordmark popping in, one after
// the next. Ported from the splash-page sketch (mind-sketches, vague-cheetah),
// which offers four arrivals; this is its `pop`.
//
// Nothing here is keyframed. A letter's turn comes, it gets poked once, and the
// overshoot and ring-down are the physics (see intro-wobble.js). Pop lands hard
// and squashes, and the surface carries a fast ripple that dies quickly.
//
// Wrapped so nothing reaches the page's global scope but the namespace below:
// shell.html loads these as classic scripts, which share one top-level lexical
// scope, so a `const` here collides with one of the same name in a sibling.

(function () {
  const { makeSpring, stepSpring, kick, makeRandom, modeStiffness, modalWarp } =
    typeof require === 'function' ? require('./intro-wobble') : window.mindsIntroWobble;
  const { LETTERS } =
    typeof require === 'function' ? require('./intro-letters') : window.mindsIntroLetters;

  const TUNING = {
    /**
     * Empty page before the first letter lands, seconds. The window opens on
     * the brand's colour and holds it for a beat, so the mark arrives on a page
     * that is already there rather than racing it.
     */
    lead: 1,
    /** Gap between one letter arriving and the next, seconds. */
    stagger: 0.1,
    scaleStiffness: 260,
    scaleRatio: 0.4,
    /** Base stiffness of the k=2 surface mode; higher modes scale up from it. */
    surfaceBase: 150,
    surfaceRatio: 0.3,
    /** Peak radial strain of the whole-body wobble, as a fraction of radius. */
    wobble: 0.11,
    /** Squash thrown at the body on arrival. */
    stretchKick: -0.18,
    stretchStiffness: 200,
    stretchRatio: 0.42,
    /** How much a stretch narrows the letter. 1 would conserve area; under that reads as jelly. */
    bulge: 0.7,
  };

  /** Which harmonics exist. 2 = ellipse, 3 = three-lobed roll, 4 = fast ripple. */
  const MODE_NUMBERS = [2, 3, 4];
  const SUBSTEP = 1 / 240;

  /**
   * Everything at rest within this is treated as settled, and the drawing
   * restored verbatim.
   *
   * Perceptual, not numerical. Mode amplitudes are a fraction of the letter's
   * radius, so 2e-3 is a fifth of a percent of ~200 units -- a third of a pixel
   * at any size this is rendered.
   */
  const REST_EPS = 2e-3;

  function createPopRig() {
    let clock = 0;
    let letters = [];
    /** Per-letter random numbers: the directions and strengths of every poke. */
    let seeds = [];

    function reset() {
      // Seeded, so the wobble directions are arbitrary but reproducible.
      const random = makeRandom(9);
      clock = 0;
      letters = LETTERS.map((letter, i) => {
        const scale = makeSpring(0, TUNING.scaleStiffness, TUNING.scaleRatio);
        const stretch = makeSpring(0, TUNING.stretchStiffness, TUNING.stretchRatio);
        const modes = MODE_NUMBERS.map((k) => ({
          k,
          cos: makeSpring(0, modeStiffness(TUNING.surfaceBase, k), TUNING.surfaceRatio),
          sin: makeSpring(0, modeStiffness(TUNING.surfaceBase, k), TUNING.surfaceRatio),
        }));
        return {
          scale,
          stretch,
          modes,
          // Every spring above, for the at-rest checks -- so adding one cannot be forgotten.
          all: [scale, stretch].concat(modes.reduce((acc, m) => acc.concat([m.cos, m.sin]), [])),
          // Centre of the letter's ink: the point its scale pivots about and the
          // point the modal field is measured from.
          cx: letter.x0 + letter.w / 2,
          cy: letter.y0 + letter.h / 2,
          born: false,
          bornAt: TUNING.lead + i * TUNING.stagger,
        };
      });
      seeds = letters.map(() => {
        const s = [];
        for (let j = 0; j < 12; j++) s.push(random());
        return s;
      });
    }

    /** The one poke a letter gets. Everything after this is ring-down. */
    function birth(L, index) {
      const s = seeds[index];
      L.born = true;
      L.scale.target = 1;
      kick(L.scale, 0.9);
      kick(L.stretch, TUNING.stretchKick);
      L.modes.forEach((m, j) => {
        // A random direction per mode: the cos/sin pair is a vector, so kicking
        // both puts the lobe at an arbitrary angle instead of always axis-aligned.
        const angle = s[j * 2] * Math.PI * 2;
        const strength = TUNING.wobble * (0.6 + 0.4 * s[j * 2 + 1]) * (j === 0 ? 1 : 0.6 / j);
        kick(m.cos, Math.cos(angle) * strength);
        kick(m.sin, Math.sin(angle) * strength);
      });
    }

    function step(dt) {
      // Fixed sub-steps: the integrator stays stable for the stiff high-order
      // modes, and a backgrounded tab cannot hand it one enormous dt.
      let remaining = Math.min(dt, 0.25);
      while (remaining > 0) {
        const h = Math.min(SUBSTEP, remaining);
        remaining -= h;
        clock += h;
        letters.forEach((L, i) => {
          if (!L.born && clock >= L.bornAt) birth(L, i);
          if (!L.born) return;
          for (const sp of L.all) stepSpring(sp, h);
        });
      }
    }

    function atRest(s) {
      return Math.abs(s.value - s.target) < REST_EPS && Math.abs(s.velocity) < REST_EPS;
    }

    /**
     * Each letter's transform, and the warp its path data needs -- null once the
     * letter is at rest, which the caller reads as "restore the drawing".
     */
    function frame() {
      return letters.map((L) => {
        // A letter that has not had its turn yet must be ABSENT, not at rest. Its
        // springs sit at zero with zero velocity, which is indistinguishable from
        // "finished" unless birth is checked first -- and a letter treated as
        // finished renders at identity, i.e. full size, before it has arrived.
        if (!L.born) {
          return {
            transform: `translate(${L.cx} ${L.cy}) scale(0 0) translate(${-L.cx} ${-L.cy})`,
            warp: null,
          };
        }
        if (L.all.every(atRest)) return { transform: 'translate(0 0)', warp: null };
        const stretch = L.stretch.value;
        const sy = L.scale.value * (1 + stretch);
        const sx = L.scale.value * (1 - stretch * TUNING.bulge);
        return {
          transform: `translate(${L.cx} ${L.cy}) scale(${sx} ${sy}) translate(${-L.cx} ${-L.cy})`,
          warp: modalWarp(L.cx, L.cy, L.modes),
        };
      });
    }

    /**
     * Snap straight to the finished word. Used for reduced motion, which must not
     * depend on a guess at the settling time -- get that wrong and "no animation
     * please" renders a frame from the middle of one.
     */
    function settleAll() {
      for (const L of letters) {
        L.born = true;
        for (const sp of L.all) {
          sp.value = sp === L.scale ? 1 : 0;
          sp.target = sp.value;
          sp.velocity = 0;
        }
      }
    }

    function settled() {
      return letters.every((L) => L.born && L.all.every(atRest));
    }

    reset();
    return {
      reset,
      step,
      frame,
      settleAll,
      settled,
      get clock() {
        return clock;
      },
    };
  }

  /**
   * How far outside the drawing's own box the arrival reaches, in the artwork's
   * own units -- the room the lockup has to leave around the mark so no part of
   * a letter is clipped while it is moving.
   *
   * Measured rather than guessed, with a little over: a letter overshoots as
   * its scale spring rings past its target and its surface modes bulge. A unit
   * test re-measures the whole run and fails if anything leaves this margin, so
   * re-tuning the physics cannot quietly start clipping the mark.
   */
  const ARRIVAL_PAD = 40;

  const introPop = { createPopRig, TUNING, ARRIVAL_PAD };

  // Loaded by shell.html with a plain script tag (the renderer has no require),
  // and required by the node unit tests.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = introPop;
  }
  if (typeof window !== 'undefined') {
    window.mindsIntroPop = introPop;
  }
})();
