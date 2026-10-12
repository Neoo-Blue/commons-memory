REM Windows: run Commons as a service with NSSM (https://nssm.cc) or WinSW.
nssm install Commons "/usr/local/bin/commons" serve --transport http --host 127.0.0.1 --port 8765
nssm set Commons AppEnvironmentExtra COMMONS_DB=%USERPROFILE%\.commons\commons.db COMMONS_REQUIRE_KEYS=1
nssm set Commons Start SERVICE_AUTO_START
nssm start Commons
REM Alternative without extra tools (runs at logon):
REM schtasks /Create /SC ONLOGON /TN Commons /TR "\"/usr/local/bin/commons\" serve --transport http --host 127.0.0.1 --port 8765"
