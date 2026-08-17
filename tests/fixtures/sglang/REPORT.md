# Minimized SGLang production replay (operator attestation fixture)

- request_success: synthetic suite finished at token budget — pass
- process_stability: production replay failed. Two concurrent delegation requests triggered HTTP 500 followed by the engine SIGQUIT handler killing the process tree; Docker recorded exit and auto-restart. — fail
- rollback: production backend rolled back to the incumbent vLLM profile — pass
