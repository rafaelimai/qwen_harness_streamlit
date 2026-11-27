# Harness — decisões pré-implementação para continuidade automática e idempotência

**Versão:** 0.2 — decisões de produto confirmadas; revisão técnica pendente
**Data:** 26/11/2026
**Estado:** decisões de produto D-03, D-05 (continuação sem verificação plena) e D-06 (autonomia controlada) **aprovadas pelo usuário**. As demais escolhas de arquitetura permanecem propostas; **não é autorização para alterar código sem revisar as decisões restantes**.
**Projeto:** [`rafaelimai/qwen_harness_streamlit`](https://github.com/rafaelimai/qwen_harness_streamlit)

## 0. Pedido ao Codex

Leia este documento juntamente com `proposta_supervisao_diagnostico_stalled_harness.md`, `contrato_validacao_maquina_estados_harness.md` e `proposta_tokens_execucao_harness.md`, se disponíveis no checkout.

**Antes de codificar:**

1. Verifique o estado **local atual** do repositório; a versão publicada de `main` pode estar defasada em relação à implementação da primeira fase.
2. Compare as regras abaixo com `worker.py`, `jobs.py`, `validation.py`, `storage.py`, `agente.py`, `app.py` e testes existentes. **Não presuma que funcionalidades descritas em MD já estejam implementadas.**
3. Apresente uma avaliação de conflitos, uma proposta de schema/migração, os limites das garantias e testes de falha determinísticos.
4. Respeite as decisões **CONFIRMADAS** na seção 4 e identifique as demais marcadas **A CONFIRMAR**. Não reduza a autonomia concedida nem amplie permissões de segurança silenciosamente.
5. Só após aprovação das decisões, proponha a implementação incremental, com mudanças pequenas e testes por etapa. Não reescreva a harness inteira.

## 1. Objetivo e escopo

Remover o clique obrigatório em **Analisar resultado**: quando um job vinculado a um turno terminar e seu resultado estiver pronto, a harness deve continuar a conversa automaticamente, mesmo com o navegador fechado, sem duplicar mensagens ou efeitos colaterais.

Preservar os princípios já definidos:

- Execução de workers separada da UI, com isolamento Docker e fila SQLite.
- Token por tentativa, *fencing* e lease conforme contrato existente; a expiração isolada do lease não torna o token obsoleto.
- Saúde operacional (`STALLED`) separada de estado de execução e validação; não recuperar/reexecutar jobs automaticamente por travamento detectado.
- Validação por critérios e regra **totalidade ou nada**; `VERIFIED` admite aceitação humana autorizada, inclusive override registrado, sem apagar evidência original.
- Nenhuma aprovação humana, dispensa de validação ou autorização de segurança será inferida a partir do evento de término de job.

**Fora do escopo imediato:** Redis, filas distribuídas, múltiplos servidores, reexecução automática de operações externas, recuperação automática de workers `STALLED`, execução exatamente uma vez de chamadas à LLM e garantia universal de efeitos externos exatamente uma vez.

## 2. Achados da arquitetura anterior a conferir no checkout

Na versão de `main` anteriormente examinada:

- `storage.py:save_messages()` substitui `conversation.json` por arquivo temporário. A substituição atômica **não previne lost updates** entre dois escritores.
- `app.py` consome `graph.stream()` na execução do Streamlit, podendo perder continuidade quando a interface é interrompida.
- `agente.py:run_python()` enfileira, `route_after_tools()` faz handoff e a UI exige **Analisar resultado** para solicitar uma nova análise.
- `jobs.py` mantém `jobs.sqlite3` com `active_token`, lease e status operacional.
- `validation.py` mantém `validation.sqlite3` separado. Em `worker.py`, o status terminal do job pode ser gravado **antes** de `record_job_result()` registrar o resultado de validação. Portanto `jobs.status` terminal, isoladamente, não comprova que a publicação está completa.

Verificar se a primeira parte implantada já modificou estes pontos. Reportar diferenças entre código real e pressupostos deste documento.

## 3. Riscos que a implementação deve eliminar

| ID | Falha | Garantia exigida |
|---|---|---|
| R-01 | UI e coordenador sobrescrevem `conversation.json` | Nenhuma mensagem persistida se perde por concorrência |
| R-02 | Usuário envia mensagem quando job termina | Ambos os eventos são preservados e ordenados de forma determinística |
| R-03 | LLM responde, mas processo cai antes do commit | Pode haver nova inferência; apenas uma saída definitiva é publicada por turno lógico |
| R-04 | Evento terminal é entregue mais de uma vez | No máximo uma requisição de continuação lógica por resultado/versionamento relevante |
| R-05 | Dois jobs terminam simultaneamente | Não existem dois escritores autorizados concorrentes para o mesmo histórico |
| R-06 | Retomada chama `run_python` outra vez | Não criar novo job como efeito acidental de replay/retry |
| R-07 | Status terminal é observado antes da validação/artefatos | Continuação só é liberada depois de uma barreira de publicação consistente |
| R-08 | Coordenador antigo retorna após perder o lock | Token obsoleto não publica mensagens, comandos nem atualizações de turno |
| R-09 | Continuação demanda aprovação humana | Estado `WAITING_FOR_USER`; não realizar `auto-approve` |
| R-10 | Uma operação externa retorna resultado incerto | Não assumir que retry é seguro; reconciliar ou exigir decisão |

## 4. Decisões de produto confirmadas e decisões técnicas em aberto

**Decisões confirmadas pelo usuário em 26/11/2026:**

| ID | Decisão aprovada | Consequência obrigatória |
|---|---|---|
| P-01 | **Sim**, a continuação automática pode criar **novas tarefas** para prosseguir o objetivo original. | Não limitar a continuação a leitura; aplicar o mesmo gate de segurança, permissões, limites e mecanismo de aprovação dos turnos normais. Nova tarefa legítima ≠ replay acidental da tarefa anterior. |
| P-02 | Se o usuário enviar uma mensagem **antes de a continuação ser reivindicada**, a mensagem humana terá prioridade. | Persistir os dois eventos, processar a mensagem primeiro e reavaliar a continuação pendente; se já reivindicada, serializar a mensagem para um próximo turno, sem preempção silenciosa. |
| P-03 | A conversa pode prosseguir mesmo com resultado **não verificado** ou **parcialmente verificado**. | Preservar os status reais, informar as limitações, nunca promover artificialmente a `VERIFIED` nem inferir um override humano. |

As propostas **A CONFIRMAR** restantes são recomendações técnicas, não decisões já aprovadas. Codex deve apresentar impactos, conflitos com código local e alternativas antes de implementá-las.

### D-01 — Fonte de verdade do histórico (**A CONFIRMAR**)

**Recomendação:** SQLite com mensagens append-only, `message_id` único, `session_id`, `turn_id`, `sequence`, `role`, payload e data/hora. Atribuição de sequência em transação; `UNIQUE(session_id, sequence)` e `UNIQUE(message_id)`. `conversation.json` passa a ser exportação/cache legada, não o destino concorrente de escritas.

**Decidir:** migrar o histórico inteiro de uma vez ou realizar migração progressiva com leitura compatível? **Recomendação:** migração explícita e idempotente por conversa, com backup/rollback; depois apenas uma fonte de verdade gravável. Não manter dois escritores ativos independentes.

### D-02 — Modelo de retomada de turno (**A CONFIRMAR**)

**Recomendação:** criar novo turno **correlacionado** ao original via `ContinuationRequest`; não depender de checkpoint interno do LangGraph, que não existe de forma durável na arquitetura anteriormente examinada. Entrada estruturada `JOB_RESULT_AVAILABLE` (não `HumanMessage`).

**Decidir:** futuramente suportar checkpoint do grafo? Pode permanecer fora do MVP.

### D-03 — Ordenação entre mensagem do usuário e resultado de job (**CONFIRMADA — P-02**)

**Recomendação:** toda entrada recebe ordem persistida na conversa. Se mensagem humana chegar **antes de o coordenador reivindicar** a continuação, tratá-la primeiro, mantendo a continuação pendente e depois reavaliando a necessidade de executá-la. Se a continuação já estiver reivindicada, enfileirar a mensagem humana para o próximo turno; não interromper silenciosamente uma resposta em produção.

**Decisão fechada:** usar esta política, sem preempção de continuação já reivindicada. A confirmação e o claim precisam ter um ponto de linearização no SQLite, para que o caso-limite seja determinístico.

### D-04 — Cardinalidade de continuações para vários jobs (**A CONFIRMAR**)

**Recomendação:** criar uma intenção por **handoff lógico** de job/resultado, com chave estável que inclua origem e versão do resultado; serializar turnos por conversa. Se dois jobs terminarem simultaneamente, ambas as intenções serão registradas, mas o coordenador poderá consolidar resultados em **um turno**, desde que registre quais eventos consumiu e não os perca.

**Decidir:** uma resposta por job ou agregação de eventos já prontos? **Recomendação:** agregação opcional e determinística por lote de resultados disponíveis antes do claim; não aguardar indefinidamente outros jobs.

### D-05 — Barreira `RESULT_READY` e política de validação (**PARCIALMENTE CONFIRMADA — P-03**)

**Recomendação:** distinguir `job.status` (execução terminal) de `result_publication_state` (resultado consumível). Publicar `JOB_RESULT_AVAILABLE` **após**:

1. aceitar o token vigente e registrar status terminal;
2. finalizar/publicar os artefatos e o manifesto (ou registrar explicitamente que estão ausentes);
3. persistir a observação/projeção de validação aplicável à **versão** do resultado, inclusive estados `UNVERIFIED`, `PARTIALLY_VERIFIED` ou falha de avaliação sinalizada;
4. gravar `RESULT_READY` e a outbox **na mesma transação do banco que detém esses dois registros**.

Como `jobs.sqlite3` e `validation.sqlite3` são separados, **não presumir uma transação única entre eles**. Um reconciliador idempotente deve conferir a evidência efetivamente persistida antes da publicação. Se o subsistema de validação estiver indisponível, decidir se a publicação aguarda ou publica um estado explícito de `VALIDATION_UNAVAILABLE` sem fingir verificação concluída. **Recomendação:** permitir resultado operacional consultável, mas só despertar o agente quando a indisponibilidade estiver explicitamente registrada como condição conhecida, jamais por ausência silenciosa de dados.

**Decisão fechada:** `VERIFIED` **não é pré-condição** para continuar; resultados `PARTIALLY_VERIFIED`, `UNVERIFIED` ou `FAILED` podem ser examinados e explicados pela continuação, sem alterar a classificação. Se a validação estiver indisponível, registrar uma condição explícita (`VALIDATION_UNAVAILABLE` ou equivalente) antes de disponibilizar a continuação; não confundir ausência de registro com avaliação concluída.

**Ainda em aberto (técnico):** timeout da etapa de validação, reconciliação entre bancos, quando formalizar `VALIDATION_UNAVAILABLE` e se algumas tarefas de alto risco exigem gate específico. A continuação não pode simplesmente ignorar a barreira de publicação.

### D-06 — Semântica de idempotência e ferramentas (**PARCIALMENTE CONFIRMADA — P-01**)

**Recomendação:** garantia de *effectively-once* **somente para efeitos internos transacionais** (mensagens aceitas, criação de continuação, publicação de resultado, registro de job com chave estável). **Não** prometer uma única chamada ao modelo ou *exactly-once* para efeitos arbitrários externos.

- Usar IDs criados pela infraestrutura, não texto livre da LLM: `source_event_id`, `continuation_id`, `turn_id`, `operation_id` e `attempt_id`.
- `UNIQUE` + gravação condicional/cas para deduplicar operações internas.
- Antes de despachar ferramenta com efeito, persistir a intenção e chave de operação. Em replay, consultar resultado anterior em vez de reemitir a ação.
- Operações sem idempotência verificável ficam em estado **`OUTCOME_UNKNOWN`** quando a conclusão não é conhecida; não repetir automaticamente.
- Ao receber `JOB_RESULT_AVAILABLE`, a continuação **pode** ler resultados, raciocinar e **criar novas tarefas necessárias ao objetivo original**. Não é apenas um turno de leitura.
- **Replay/retry da mesma operação lógica**: reaproveitar o `operation_id` e o resultado já registrado, sem gerar novo `job_id` somente porque a LLM regenerou a chamada.
- **Nova tarefa legítima**: receber `operation_id` novo, vinculado à continuação/objetivo, com motivo e trilha auditável. Um `operation_id` novo não deve ser inventado apenas para contornar deduplicação.
- **Segurança não é substituída por autonomia:** respeitar `security.py`, os gates de aprovação e as autorizações existentes; operações que antes exigiam aprovação continuam exigindo. Jamais transformar evento operacional em aprovação humana.
- **Evitar loops:** definir política para continuação que inicia novo job, recebe resultado e inicia outro job; limites de profundidade, orçamento e critérios de parada continuam **a especificar**, e não são autorização para execução ilimitada.

**Decisão fechada:** novas tarefas automáticas são permitidas com as mesmas regras de segurança e aprovação dos turnos normais; não exige confirmação adicional *apenas por ser uma continuação*, se a ferramenta já estiver autorizada pelas políticas atuais.

**Ainda em aberto (técnico):** classificação das ferramentas e escopo máximo da autonomia, deduplicação sob regenerações não determinísticas, limites de ciclos e orçamento, reconciliação de operações com resultado incerto.

### D-07 — Retry e falha da continuação (**A CONFIRMAR**)

**Recomendação inicial:** até três tentativas **do processamento de continuação** com backoff limitado e erros registrados; nunca repetir o job apenas porque a continuação falhou. Depois, marcar `CONTINUATION_ERROR` e permitir intervenção/reanálise manual. Falhas com efeitos externos incertos **não** entram no retry genérico.

**Decidir:** número de tentativas, intervalos e expiração; definir limites separados para erros transitórios, determinísticos e incerteza de execução.

### D-08 — Topologia do coordenador (**A CONFIRMAR**)

**Recomendação:** processo independente `turn_coordinator.py`, iniciado junto aos serviços da harness e capaz de continuar sem navegador aberto. Usar SQLite transacional para arbitragem por conversa; processo pode ser reiniciado pelo launcher/supervisor existente. Não executar inferência longa sob transação SQLite.

**Decidir:** execução via `tmux` atual ou `systemd`, e ciclo de vida do coordenador quando a UI fecha.

## 5. Contratos e invariantes sugeridos

### 5.1. Entidades mínimas (nomes ilustrativos)

```text
ConversationEvent       {event_id, session_id, sequence, type, payload_ref, created_at}
ConversationMessage     {message_id, session_id, turn_id, sequence, role, payload, created_at}
ContinuationRequest     {continuation_id, session_id, source_event_id, source_turn_id,
                         job_id, result_version_id, status, attempts, created_at}
ConversationLease       {session_id, owner_id, fencing_generation, expires_at}
TurnAttempt             {turn_id, attempt_id, session_id, fencing_generation,
                         status, last_error, started_at, finished_at}
ToolOperation           {operation_id, turn_id, tool_name, operation_key,
                         status, result_ref, created_at}
ResultPublication       {job_id, result_version_id, publish_status,
                         validation_ref, artifact_manifest_ref, published_at}
OutboxEvent             {event_id, aggregate_id, kind, payload_ref, status, created_at}
```

Os nomes e tabelas podem ser consolidados. Separar **identidade lógica** da continuação de **tentativas físicas** do coordenador.

### 5.2. Máquina de estados da continuação

```text
WAITING_FOR_JOB
    └──(RESULT_READY + outbox)──> RESUME_PENDING
RESUME_PENDING
    └──(claim com fencing)──> RESUMING
RESUMING
    ├──(resposta persistida e turn finalizado)──> COMPLETED
    ├──(nova tarefa enfileirada)──> WAITING_FOR_JOB
    ├──(solicita aprovação/decisão)──> WAITING_FOR_USER
    ├──(erro seguramente repetível)──> RETRY_PENDING
    └──(erro não repetível/limite)──> CONTINUATION_ERROR
RETRY_PENDING ──(nova tentativa segura)──> RESUME_PENDING
WAITING_FOR_USER ──(entrada humana autorizada)──> RESUME_PENDING
```

`STALLED` pertence à dimensão de saúde operacional, **não** é estado terminal nem autoriza retry. `VERIFIED` pertence à aceitação dos resultados e não equivale a `COMPLETED` da continuação.

### 5.3. Invariantes obrigatórios

- **INV-01:** um evento terminal repetido não cria uma segunda intenção de continuação lógica.
- **INV-02:** nenhuma continuação é liberada com referências a artefatos/validação ainda não persistidos ou com integridade desconhecida sem sinalização explícita.
- **INV-03:** em uma conversa, somente um coordenador com *fencing generation* vigente pode confirmar mensagens e operações.
- **INV-04:** toda mensagem recebida é persistida sem sobrescrever mensagens anteriores; atualizações são append-only ou com revisão transacional.
- **INV-05:** uma mensagem humana e um evento de job concorrentes não se perdem, mesmo que a ordem de processamento mude.
- **INV-06:** uma resposta da LLM pode ser regenerada, mas apenas uma resposta definitiva por `turn_id` é publicada; respostas parcialmente transmitidas devem ser identificadas como não confirmadas.
- **INV-07:** a confirmação do consumo da continuação e a publicação das mensagens finais ocorrem na mesma transação local, ou usam protocolo de recuperação demonstravelmente equivalente.
- **INV-08:** replay não dispara operação com efeito colateral sem verificar a chave persistida da operação. Um `operation_id` novo não pode ser criado apenas porque a mesma ação foi sugerida em nova geração da LLM. Uma tarefa realmente nova, exigida pela continuidade do objetivo e autorizada pelas policies, permanece permitida.
- **INV-09:** token de worker obsoleto nunca publica novo resultado vigente nem desperta continuação.
- **INV-10:** evento operacional **nunca** é escrito como `HumanMessage`; a LLM recebe entrada estruturada, de origem verificável.
- **INV-11:** status de validação parcial, não verificado ou reprovado permanece fiel ao contrato, mesmo após retomada conversacional. É permitido prosseguir e explicar o resultado, mas nunca afirmar `VERIFIED` sem base ou decisão humana registrada.
- **INV-12:** UI não precisa estar aberta para disparar continuação; UI apenas consulta e apresenta estado persistido.

### 5.4. Publicação e deduplicação — protocolo sugerido

```text
Worker termina tentativa
  → aceita o token vigente e persiste resultado operacional
  → finaliza artefatos/manifesto
  → registra observação de validação vinculada à versão
  → reconciliador confere referências e marca RESULT_READY
  → transação: RESULT_READY + OutboxEvent único
  → coordenador: consome evento, recupera contexto e reivindica conversa
  → persiste início de TurnAttempt e intenção de ferramenta, quando houver
  → executa inferência (fora de transação)
  → transação: valida fencing + grava mensagem(ens) definitivas + conclui continuação
  → UI apresenta status e novas mensagens
```

**Observação:** o resultado operacional deve ficar consultável mesmo antes de `RESULT_READY`, com sinalização de publicação pendente. Se a finalização entre bancos/arquivos falhar, a reconciliação deve retomar o protocolo sem duplicar o efeito.

## 6. Sequência de implementação sugerida

| Ordem | Entrega | Condição de saída |
|---|---|---|
| 0 | Revisão do código local e decisões D-01 a D-08 | Schema, garantias, plano de migração e política de ferramenta aprovados |
| 1 | Persistência de mensagens e migração de `conversation.json` | Testes concorrentes sem perda de atualização; leitura legada mantida conforme decisão |
| 2 | Barreira `RESULT_READY` + reconciliação + outbox | Nenhuma continuação nasce de publicação parcial; evento terminal repetido é deduplicado |
| 3 | `ContinuationRequest` e IDs estáveis de operação | Um resultado lógico gera no máximo uma continuação pendente |
| 4 | Lock/fencing por conversa | Dois coordenadores disputam; somente um consegue publicar |
| 5 | `turn_coordinator.py` + entrada estruturada | Retomada sem UI nem mensagem humana falsa |
| 6 | Persistência da decisão de ferramentas + política anti-replay | Crash/retry não cria job ou operação nova inadvertidamente |
| 7 | Retry limitado, estados de falha e reconciliação | Falhas transitórias se recuperam; casos ambíguos não são repetidos cegamente |
| 8 | Chat com status automático e respostas persistidas | Aba **Tarefas** vira painel detalhado; **Analisar resultado** deixa de ser requisito |
| 9 | Testes de falha e carga local | Cenários adversariais abaixo passam com reprodutibilidade |

A ordem entre as etapas 1 e 2 pode ser ajustada conforme dependências reais, mas **não habilitar retomada automática antes de resolver perda de mensagens e publicação parcial**.

## 7. Testes de aceitação imprescindíveis

1. **Duas escritas simultâneas:** UI e coordenador gravam eventos/mensagens diferentes; nenhuma entrada desaparece.
2. **Humano + job simultâneos:** preservar ambos, com ordenação determinística e observável.
3. **Evento terminal duplicado:** mesmo evento entregue 10 vezes; uma só continuação lógica.
4. **Dois jobs simultâneos:** uma conversa, duas conclusões; nenhuma continuação concorrente consegue publicar em paralelo.
5. **Crash após resposta da LLM, antes de commit:** pode haver nova inferência, mas uma única resposta final confirmada.
6. **Crash depois de commit, antes de ACK:** reinício reconhece a continuação concluída e não publica outra resposta.
7. **Coordinator A obsoleto:** A perde lease para B e tenta salvar resposta; atualização rejeitada.
8. **Crash entre `jobs.sqlite3` e `validation.sqlite3`:** reconciliador resolve publicação sem expor resultado como pronto prematuramente.
9. **Crash após artefato no disco, antes de manifesto:** reconciliação detecta situação e não presume que o efeito já foi aceito.
10. **Replay com `run_python`:** mesmo evento lógico não cria job adicional; operação incerta não sofre retry automático.
11. **Validação parcial:** continuação informa limites, sem mudar status para `VERIFIED` nem efetuar override.
12. **Aprovação pendente:** nenhum processo consegue autorizar implicitamente ação sensível, mesmo quando a continuação pode criar novos jobs.
13. **Streamlit desligado:** coordenador processa continuação e mensagens aparecem ao reabrir.
14. **Job sem continuação:** evento visível, sem invocação espontânea da LLM.
15. **Token de worker `stale`:** evento histórico não torna resultado vigente nem acorda a conversa.
16. **Logs/resultados maliciosos:** conteúdo não é tratado como instrução de usuário/sistema; controles de ferramentas preservados.
17. **Continuação inicia nova tarefa:** ao receber `JOB_RESULT_AVAILABLE`, o agente pode criar um job adicional genuinamente necessário, sujeito ao gate de segurança existente; a execução não exige clique em *Analisar resultado*.
18. **Replay da continuação criadora:** reiniciar após criação do job adicional não cria um segundo job para a mesma operação lógica, mesmo que a LLM regere a intenção.
19. **Mensagem humana antes do claim:** evento humano tem prioridade; resultado do job permanece disponível e não é perdido. Se a continuação já foi reivindicada, o usuário entra na fila e será processado sem sobrescrita.
20. **Validação não conclusiva:** `UNVERIFIED`/`PARTIALLY_VERIFIED` ou `VALIDATION_UNAVAILABLE` explicitamente registrada podem originar uma continuação; a conversa informa a limitação sem mudar o status nem aprovar implicitamente.
21. **Limites de autonomia:** uma cadeia de jobs disparada pela continuação não pode ignorar gate de segurança nem ultrapassar o limite de ciclos/orçamento que vier a ser aprovado.

Usar relógio injetável, duas conexões SQLite reais, pontos de falha determinísticos e diretórios temporários; não depender de longos `sleep()` ou timing casual. Diferenciar testes unitários, testes transacionais e testes de integração com Docker/LLM simulada.

## 8. Pontos em aberto que Codex deve devolver como perguntas/propostas

As decisões **D-03, D-05 (aspecto de produto) e D-06 (aspecto de produto)** já estão confirmadas. As escolhas técnicas remanescentes de D-01 a D-08 continuam no **gate de desenho**. Além delas, verificar:

- Como obter/produzir `result_version_id` estável, especialmente com validação em banco separado.
- Como representar a indisponibilidade operacional do subsistema de validação (status distinto de verificação), qual timeout e qual fallback explícito; já está decidido que falta de validação não impede, por si só, a continuação.
- Quais ferramentas são apenas leitura e quais têm efeito colateral; como impor política no backend (não só no prompt). **Novas tarefas automáticas já estão autorizadas**, desde que respeitem gates existentes.
- Como migrar, manter backup e fazer rollback de sessões antigas e jobs já em andamento.
- Como lidar com respostas parciais via streaming já exibidas na UI quando o turno é reiniciado.
- Qual o comportamento quando uma mensagem humana contradiz o objetivo original durante uma continuação pendente.
- Quais métricas/alertas demonstram stuck coordinator, outbox acumulada ou erros repetitivos, sem misturar esse incidente à saúde do worker.
- Qual a estratégia de retenção dos eventos/outbox e o comportamento sob disco cheio/erro SQLite.
- Como evitar que uma operação de reconciliação crie ciclos infinitos de continuação; como definir limite de profundidade, tempo/custo e orçamento para *novas tarefas legítimas* criadas automaticamente.

## 9. Resposta esperada do Codex **antes de codificar**

Devolver objetivamente:

1. **Mapa de diferenças** entre este documento e o código real atual (arquivo/função/risco).
2. **Tabela de decisões** D-01 a D-08: marcar explicitamente P-01, P-02 e P-03 como aprovadas, e apresentar recomendação, alternativa e justificativa apenas para escolhas técnicas ainda pendentes.
3. **Esquema mínimo** de tabelas/índices, transações e regras de migração.
4. **Fluxo de recuperação** para cada janela de crash (antes/depois de persistir resultado, outbox, resposta e ACK).
5. **Política de ferramentas e replays**, explicitando o que é seguro repetir e o que entra em `OUTCOME_UNKNOWN`.
6. **Plano de PRs pequenos**, cada um com testes de aceitação associados.
7. **Questões não resolvidas**, sem reabrir P-01/P-02/P-03 sem conflito concreto e sem tratar suposições técnicas como decisões aprovadas.

**Critério de aceitação do desenho:** qualquer etapa que prometa “executar exatamente uma vez” deve especificar **qual efeito**, **onde é persistido**, **qual é a chave idempotente**, **qual é a fronteira transacional** e **o que ocorre se o processo cair em cada lado dessa fronteira**. Para chamadas a modelos ou sistemas externos, declarar explicitamente as limitações.
