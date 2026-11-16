# Instrucciones del proyecto

## Subagentes de EGM (Elephant-Goldfish Model)

- Lanza siempre los subagentes de EGM (`egm-*`: synthesizer, design, zoom-out, comprehension, codebase-details, implementation-readiness, structural, inventive-catalyst, style, bootstrap-orchestrator) con `model: "sonnet"` en la llamada a `Agent`, para gastar menos tokens.
- Los revisores que lanza `egm-synthesizer` o `egm-bootstrap-orchestrator` heredan el modelo (`model: inherit`), así que basta con lanzar esos dos en Sonnet.
- Esta regla no afecta a la sesión principal (Elephant), que sigue con el modelo elegido.
