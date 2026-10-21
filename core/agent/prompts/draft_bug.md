Categories: "bug" and "perf" only.

- bug: code that produces wrong results, crashes, or misbehaves on realistic input. Examples: off-by-one errors, wrong comparison operator, unhandled None/null, a missing `await`, wrong timezone handling, swapped arguments, an exception swallowed silently, a resource never closed.
- perf: a clear, significant inefficiency introduced by this PR. Examples: a database query inside a loop, repeated work that could be hoisted out of a loop, quadratic behaviour on input that can be large.

Good comment: "`apply_discount` can return a negative total when the discount exceeds the subtotal" with a patch clamping at zero.
Bad comment: "Consider adding error handling here" (vague, no concrete failure).
Bad comment: "This function could be faster" (no evidence the input is large).
