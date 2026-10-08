"""Adaptadores de integração do pipeline com sistemas externos (Django, Celery).

Cada adaptador expõe a configuração canônica de ``src.config`` e as funções
públicas do ciclo como uma caixa preta: nada de regra de negócio aqui, só
tradução de fronteira — montar a configuração, abrir a conexão própria do
ciclo, chamar a função pública e devolver o dicionário de resultado.
"""
