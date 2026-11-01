# Gates: OTC Research Improvements

Scope: All 4 research-driven improvements implemented and deployed.

- [x] G1: All leaf gates pass
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && python3 -c "import py_compile; py_compile.compile('strategies/omega_complete.py', doraise=True); py_compile.compile('core/option_chain.py', doraise=True); print('ALL COMPILE OK')"
  EXPECT: ALL COMPILE OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=0975c860aec1b4ec617a9052273c98b87328b0a9a5d432b8e32807a192d03c40; exit=0; EXPECT=matched; output-sha256=8179116cb6193b85c2ad7373e291ad67244b22e6c48b5a4556d782579a6f1fac; output-bytes=15; shell=/bin/sh; cwd=/Users/vishnu/Desktop/pyhton p/omega-trading-system/.unlazy/otc-improvements; path=41dea34fa2cd/17 entries

- [x] G2: Bot running on VPS
  CHECK: ssh -i ~/.ssh/oracle_vps.key -o ConnectTimeout=30 ubuntu@129.225.76.88 "sudo systemctl is-active omega-trading.service"
  EXPECT: active
  EVIDENCE: automatic-evidence=v1; definition-sha256=f2760cd15adda66a031432fc9e78c07be398c9cd6fa57fa621551f1d63300209; exit=0; EXPECT=matched; output-sha256=45df5ad5e0ecfa54d3226343e0e6857337494ba6e32f189d1174070665d8c659; output-bytes=7; shell=/bin/sh; cwd=/Users/vishnu/Desktop/pyhton p/omega-trading-system/.unlazy/otc-improvements; path=41dea34fa2cd/17 entries
