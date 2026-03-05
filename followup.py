from utils import call_ollama

def answer_followup(question, context_chunks):

    context = "\n\n".join(context_chunks)

    prompt = f"""
다음 설교 내용들을 참고하여 질문에 답하라.

[설교 내용]
{context}

[질문]
{question}
"""

    return call_ollama(prompt)