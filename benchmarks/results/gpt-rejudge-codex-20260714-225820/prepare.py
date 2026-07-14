import json, hashlib, os
from pathlib import Path
import sys
sys.path.insert(0, str(Path('src').resolve()))
from joiny_mnemonic.longmemeval import load_dataset
rows_path=Path('benchmarks/results/longmemeval-latest.jsonl')
dataset_path=Path(r'R:/Projects/data/longmemeval_s_cleaned.json')
out=Path(os.environ['OUT_DIR'])
rows=[json.loads(line) for line in rows_path.read_text(encoding='utf-8').splitlines() if line.strip()]
questions={item.question_id:item for item in load_dataset(dataset_path)}
items=[]
for row in rows:
    item=questions[row['question_id']]
    items.append({
        'question_id': row['question_id'],
        'question_type': item.question_type,
        'question': item.question,
        'gold_answer_or_rubric': item.answer,
        'model_answer': row['answer'],
        'original_correct': bool(row['correct']),
        'retrieval_hit': row.get('retrieval_hit'),
        'gold_coverage': row.get('gold_coverage'),
        'context_tokens': row.get('context_tokens'),
    })
(out/'input_all.jsonl').write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in items)+'\n', encoding='utf-8')
for i in range(0, len(items), 20):
    batch=items[i:i+20]
    (out/f'batch_{i//20:03d}.jsonl').write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in batch)+'\n', encoding='utf-8')
meta={
    'source_rows': str(rows_path),
    'dataset': str(dataset_path),
    'dataset_sha256': hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
    'source_jsonl_sha256': hashlib.sha256(rows_path.read_bytes()).hexdigest(),
    'items': len(items),
    'batch_size': 20,
}
(out/'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
print(out)
