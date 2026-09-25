# Examples

One script per integration, the same code the documentation shows. Each needs its extra
(`pip install 'niadra[livekit]'`, for instance), a Niadra source key in `NIADRA_API_KEY` and the
provider's own key. To try them without Niadra's cloud, start the local emulator with
`niadra-mock` and set `NIADRA_BASE_URL=http://127.0.0.1:8765`.

The tests under `tests/integrations/` run the same wiring against the emulator with each
framework's real types and a scripted model, in CI.
