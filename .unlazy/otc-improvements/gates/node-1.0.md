# Gates: Deploy integration

Scope: Push all changes to VPS and restart bot successfully.

- [ ] G1: Bot running on VPS with new code
  CHECK: ssh -i ~/.ssh/oracle_vps.key -o ConnectTimeout=30 ubuntu@129.225.76.88 "sudo systemctl is-active omega-trading.service"
  EXPECT: active
  EVIDENCE: pending
