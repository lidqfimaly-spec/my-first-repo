# 第 0 阶段文件清单

以下均为本阶段创建的真实绝对路径，没有修改任何旧项目文件。

## 根目录

- `/Users/lidequan/PythonProject/Zevorinth/.gitignore`
- `/Users/lidequan/PythonProject/Zevorinth/.env.example`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/pyproject.toml`

## 代码与测试

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/__init__.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/__main__.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cli.py`

## 文档

- `/Users/lidequan/PythonProject/Zevorinth/docs/environment-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/reuse-matrix.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/acceptance-criteria.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/research-plan.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

---

# 第 3.41 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_planner_instructions.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.41-llm-planner-instructions-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.41-post-fix-single-call-verification.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.41/stable-summary.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.41/mutations.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.41/mutations-rerun.json`

---

# 第 3.42 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/tests/test_candidate_diagnostics.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.42-candidate-diagnostics-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.42/pilot-result.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.42/stable-summary.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.42/mutations.json`

---

# 第 3.43 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.43-diagnostics-contract-test-isolation-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_candidate_diagnostics.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_timeout_cancel.py`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.43/stable-summary.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.43/mutations.json`

---

# 第 3.44 阶段任务书与执行审计

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.44-end-to-end-verification-taskbook.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.44-end-to-end-verification-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 状态

- 真实模型 API 请求：1（授权上限 1）；
- 候选协议：通过；
- 执行链：BLOCKED（`isolation_unverified / evidence_id_missing`）；
- 容器：未启动；
- 本地结果：`artifacts/audits/phase-3.44/pilot-result.json`；
- 本地摘要：`artifacts/audits/phase-3.44/stable-summary.json`。

---

# 第 3.45 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/tests/test_trusted_test_parameters.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.45-trusted-parameter-contract-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.45/offline-e2e.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.45/mutations.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.45/stable-summary.json`

---

# 第 3.46 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.46-real-model-end-to-end-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 真实调用结果

- 真实模型 API 请求：1（授权上限 1）；
- 候选：`run_tests` / `test_ok`，校验通过；
- provisioning：7 项探针；
- 容器：`zv-049b3b52ef37-02542e1d`，退出码 0；
- `TestResultVerifier`：`tests_passed`；
- 任务：`succeeded`。

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.46/pilot-result.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.46/stable-summary.json`

---

# 第 3.47 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.47-execution-plan-traceability-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_trusted_test_parameters.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_lifecycle.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 本地审计产物（被 `.gitignore` 排除）

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.47/offline-e2e.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.47/mutations.json`
- `/Users/lidequan/PythonProject/Zevorinth/artifacts/audits/phase-3.47/stable-summary.json`

---

# 第 2.5 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/online_pilot_v1.json`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.5-preflight.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.5-online-results.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/.env.example`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2-results.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evaluation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_planner_eval.py`

---

# 第 2.6 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/response_normalization.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_response_normalization.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.6-response-hardening.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.6-results.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evaluation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/planner_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_planner_eval.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.5-online-results.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

---

# 第 2.7 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/online_retest_v1.json`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.7-preflight.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2.7-results.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evaluation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_planner_eval.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

在线结果路径不属于源码清单：

`/Users/lidequan/PythonProject/Zevorinth/artifacts/evaluations/phase-2.7-targeted-retest-v1.json`

该文件已由批准的定向复测生成，位于被忽略的运行产物目录；不纳入本次源码变更清单。

---

# 第 3.2 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_task_conversation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_cli.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3-design.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_schema.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

Schema version 保持 3；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.3 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/intent.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.3-design.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_intent.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_clarification_workflow.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_schema.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

Schema version：4。运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.40 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.40-single-call-real-model-closure-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.39-run-tests-protocol-extension-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_run_tests_capability.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.38-llm-planner-run-tests-capability-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_run_tests_capability.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.37-isolation-evidence-provisioning-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evidence_provisioning.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_evidence_provisioning.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.36-real-model-execution-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.35-real-model-pilot-rerun.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.34-fsize-budget-recovery-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_timeout_cancel.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_container.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.33-resource-limits-failure-recovery-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_timeout_cancel.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.32-container-timeout-cancel-artifact-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_timeout_cancel.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.31-container-lifecycle-artifat-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_lifecycle.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/policy.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/execution_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_local_test_execution.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_container.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/user_acceptance_runner.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.30-run-tests-container-execution-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_container_live.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/execution_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_local_test_execution.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_container.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/user_acceptance_runner.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.29-run-tests-container-migration-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_run_tests_container.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.28-isolation-evidence-persistence-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evidence_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_evidence_store.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.27-isolation-evidence-lifecycle-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_evidence_lifecycle.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation_evidence.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_live.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.26-container-isolation-verification-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation_evidence.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_isolation_evidence.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_live.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---



## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.26-container-isolation-verification-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation_evidence.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_isolation_evidence.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.25 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.25-container-isolation-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_container_backend.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.24 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.24-isolation-backend-decision-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_backend_boundaries.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_read_confinement_and_lifecycle.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.23 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.23-read-confinement-process-lifecycle-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_read_confinement_and_lifecycle.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.22 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.22-isolation-boundary-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_durable_intent_and_isolation.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.21 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.21-os-isolation-durable-safety-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/isolation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_durable_intent_and_isolation.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.20 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.20-cleanup-quarantine-recovery-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cleanup_reconciliation.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_cleanup.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/side_effect_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cleanup_races.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.19 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.19-hardened-cleanup-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cleanup_races.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_cleanup.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/side_effect_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_workspace_cleanup.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.18 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.18-workspace-effect-governance-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_effects.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_cleanup.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_workspace_cleanup.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_snapshot.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/side_effect_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_workspace_side_effects.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.17 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.17-workspace-side-effect-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/workspace_snapshot.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_workspace_side_effects.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/side_effect_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_side_effect_audit.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/execution_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_local_test_execution.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.16 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.16-controlled-test-execution-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/local_test_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_local_test_execution.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/execution_audit_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_execution_audit.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.15-offline-user-acceptance-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/policy.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/audit_report.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/user_acceptance_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_user_acceptance.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.15 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.15-offline-user-acceptance-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/user_acceptance_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_user_acceptance.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/audit_report.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.14 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.14-lifecycle-evidence-reproducibility-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/audit_report.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_audit_reproducibility.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_lifecycle_provenance.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_mutation_runner.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.13 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.13-e2e-provenance-compound-mutation-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_e2e_provenance.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/safety_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/e2e_acceptance_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/eval_compare.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_acceptance_metrics.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_eval_compare.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_execution_budget.py`

运行产物不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.12 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.12-reproducible-gates-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/eval_compare.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_coverage_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_effect_provenance.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_eval_compare.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evaluation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/safety_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_execution_budget.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_mutation_runner.py`

运行产物和 coverage 数据不纳入源码提交；Schema 版本保持 v4。

---

# 第 3.11 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.11-metric-coverage-mutation-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_cases.json`
- `/Users/lidequan/PythonProject/Zevorinth/tests/mutation_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_acceptance_metrics.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_coverage_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_mutation_runner.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/safety_gate_v1.json`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/safety_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/e2e_acceptance_runner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_safety_gate.py`

运行产物和 coverage 数据不纳入源码提交。

---

# 第 3.10 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.10-safety-gate.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/safety_gate_v1.json`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/safety_gate.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_safety_gate.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/offline_release_check.py`

运行报告位于 `artifacts/audits/phase-3.10-safety-gate-v3.json`，不纳入源码提交。

---

# 第 3.9 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.9-real-model-pilot.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

本地运行产物：

- `artifacts/evaluations/phase-3.9-targeted-pilot-v1.json`，不纳入源码提交。

---

# 第 3.7 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.7-release-readiness.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_release_readiness.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/offline_release_check.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/e2e_acceptance_runner.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.5 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.5-design.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.6-acceptance.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/context.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_context_continuity.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cooperative_cancellation.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/e2e_acceptance_runner.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/intent.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/state_machine.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.6 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.6-execution-budget-recovery-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_execution_budget.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_staging_cleanup.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/state_machine.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_concurrency.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_crash_consistency.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/staging_cleanup_worker.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.5 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.5-lease-renewal-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/lease_renewal.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_lease_renewal.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.4 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.4-lease-takeover-coordination-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.3-side-effect-fencing-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/artifact_tool.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/crash_consistency_worker.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_crash_consistency.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_side_effect_fencing.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.3 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.3-side-effect-fencing-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_side_effect_fencing.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/artifact_tool.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/crash_consistency_worker.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_artifact_tool.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_crash_consistency.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_verifier.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.2 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.2-crash-consistency-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/tests/crash_consistency_worker.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_crash_consistency.py`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_clarification_workflow.py`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 3.4.1 阶段新增与修改

## 新增

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-3.4.1-audit.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/conversation_service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_conversation_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_clarification_workflow.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

Schema version 保持 4；运行产物和 `.idea/` 不纳入源码清单。

---

# 第 2 阶段新增与修改

## 新增源码与评测数据

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planning_validation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evaluation.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/evals/planner_cases.json`

## 新增测试

- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_planner_eval.py`

## 新增文档

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2-llm-planner.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-2-results.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/.env.example`
- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/pyproject.toml`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/__init__.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/state_machine.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_llm_planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_planner_eval.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_state_machine.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1-implementation.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1.5-results.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

## 运行目录说明

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/data/README.md`

## 目录树

```text
Zevorinth/
├── .env.example
├── .gitignore
├── README.md
├── pyproject.toml
├── artifacts/
│   └── README.md
├── data/
│   └── README.md
├── docs/
│   ├── acceptance-criteria.md
│   ├── architecture.md
│   ├── environment-audit.md
│   ├── execution-log.md
│   ├── file-manifest.md
│   ├── research-plan.md
│   └── reuse-matrix.md
├── src/
│   └── zevorinth/
│       ├── __init__.py
│       ├── __main__.py
│       └── cli.py
└── tests/
    └── test_cli.py
```

---

# 第 1 阶段新增与修改

## 新增源码

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/state_machine.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/planner.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/policy.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/artifact_tool.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`

## 新增测试

- `/Users/lidequan/PythonProject/Zevorinth/tests/test_state_machine.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_artifact_tool.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_verifier.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_acceptance.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/process_exit_helper.py`

## 新增文档

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1-implementation.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1-results.md`

## 修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`

Phase 0 的环境核查、复用矩阵、研究计划与原始验收标准均保留。

---

# 第 1.5 阶段新增与修改

## 新增测试与测试辅助

- `/Users/lidequan/PythonProject/Zevorinth/tests/sigkill_worker.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_sigkill.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_concurrency.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_hardening.py`

## 新增文档

- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1.5-audit.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1.5-results.md`

## 核心修改

- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/models.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/event_store.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/service.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/src/zevorinth/__init__.py`
- `/Users/lidequan/PythonProject/Zevorinth/pyproject.toml`

## 测试修改

- `/Users/lidequan/PythonProject/Zevorinth/tests/process_exit_helper.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_acceptance.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_cli.py`
- `/Users/lidequan/PythonProject/Zevorinth/tests/test_event_store.py`

## 文档修改

- `/Users/lidequan/PythonProject/Zevorinth/README.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/architecture.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1-implementation.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/phase-1-results.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/execution-log.md`
- `/Users/lidequan/PythonProject/Zevorinth/docs/file-manifest.md`
