Category: "test_gap" only.

- test_gap: new or changed behaviour in this PR that has an important, specific case no test covers, where a bug is likely. Name the exact case. Only comment if the PR changes non-test code, or changes a test so that it no longer checks what it used to.

Good comment: "`split_bill` now rounds per person, but no test covers a total that doesn't divide evenly (e.g. 10 between 3)".
Good comment: "This test's expected value was changed to match the new output, so it no longer detects the rounding bug it was written for".
Bad comment: "Add more tests" (no specific case).
Bad comment: asking for tests of trivial getters or constants.
