Category: "security" only.

- security: a concrete vulnerability introduced by this PR. Examples: SQL/command/template injection from user input, path traversal, secrets or tokens hard-coded or logged, disabled TLS verification, unsafe deserialization (pickle, yaml.load), missing authorization checks, user input passed to eval/exec.

Good comment: "User-supplied `order_id` is formatted directly into the SQL string, allowing SQL injection" with a patch using a parameterized query.
Bad comment: "Make sure this is secure" (no specific attack).
Bad comment: flagging a hard-coded value that is clearly a test fixture.
