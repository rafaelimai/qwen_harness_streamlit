# Próximas capacidades

Implementado nesta versão:
- write_file para texto/código com extensão preservada.
- run_python, get_job_status, cancel_job; worker separado e fila SQLite.
- Contêineres por execução, sem rede, com limites e snapshots das entradas.
- ask_user para perguntar e encerrar o turno; opções clicáveis e resposta pelo chat.
- Conversas nomeadas e persistidas; downloads individuais e ZIP pelo navegador.

Ainda não implementado:
- Checkpoints duráveis do LangGraph para retomar no ponto interno de um nó.
  O histórico da conversa e os jobs persistem; jobs ambíguos não são repetidos.
- Continuação automática do agente quando um job acaba. Use Analisar resultado.
- Gestão de contexto/token budget e resumo automático de histórico.
- Testes/evals com seu Qwen real: uso correto de ferramentas, seleção de parâmetros,
  respostas a erros e qualidade dos resultados.
- Execução de projetos/repositórios completos, aplicação de patches, diffs e versionamento.
- Busca semântica/RAG (a busca atual é literal) e OCR dedicado.
- Autenticação e múltiplos usuários com autorização individual.
- Retenção configurável e limpeza de snapshots/logs antigos.
- Ferramentas SQL: primeiro catálogo/schema e consultas com conta somente leitura,
  permissões por tabela/view, timeout, parâmetros e limite de linhas. Credenciais
  fora dos prompts; alterações de dados seriam uma capacidade separada.

ask_user não depende de interrupt(): o ToolNode executa a ferramenta, um nó de
handoff apresenta a pergunta e encerra o grafo. Sua próxima mensagem é o próximo
turno, com o histórico anterior. Isto é suficiente para esclarecer um requisito.
Para pausar/resumir exatamente um workflow em andamento, adicionar checkpointer,
thread_id estável e interrupt()/Command(resume=...), com efeitos idempotentes.
