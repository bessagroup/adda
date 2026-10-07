---
id: verify-before-you-trust
title: Verify before you trust — a model you built must reproduce answers known without it
tags: [verification, validation, correctness, oracle, surrogate, model, check, expected, tolerance]
audience: [datagenerator, implementer]
feature: model_verification
---
A computational part you built, such as an oracle or a surrogate, produces
numbers that later become evidence. Those numbers count as evidence only after
the part has reproduced answers that are known independently of its own code.

A part that runs, returns finite numbers, and has the right signature has passed
an interface check. It can still be wrong: a misplaced boundary condition or
load, a swapped index, a wrong unit, or a wrong sign all produce clean-looking
output. Comparing the model with itself cannot find these errors, because the
same mistake is on both sides of the comparison. The expected value must come
from outside the code.

## What makes an expectation independent
- You obtained it without running your model: a closed-form result, a limit
  case, a symmetry, a conservation law, a published value, or a result
  from a different method.
- Or you obtained it by changing the model in a way that has a known effect: a
  refinement that must converge, or a rescaling that must leave the answer
  unchanged.

If you cannot name where the expected value comes from, you have not run a
check.

## How to run it
- Choose cases whose answer you know independently of your model, and that
  exercise the parts of the model that the real inputs will exercise.
- Set the tolerance before you look at the result, and write down why it has
  that size.
- Call the model directly. These calls are not oracle evaluations.
- Record the expected value, its source, the obtained value, the tolerance, and
  pass or fail, one entry per check.

## When a check fails
Report the failure with the numbers. Fix the model, then run the check again.
Do not widen the tolerance to make the check pass, and do not drop the check.
If you find that the expectation itself was wrong, say why, and record the
correction, before you change it. Never change an expected value after you have
seen the result without that record.
A part with an open failed check stays labelled as unverified in your report.

## A surrogate
For a surrogate, the independent expectation is data the fit did not see
(held-out or cross-validated error), plus any limit or invariance the true
function is known to have. See [[surrogates-are-off-ledger]].
