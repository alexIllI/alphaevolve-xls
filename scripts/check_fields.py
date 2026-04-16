from alphaevolve.evaluator import Evaluator
from alphaevolve.ppa_metrics import PPAMetrics
m = PPAMetrics()
print("critical_path_ps:", m.critical_path_ps)
print("pipeline_reg_bits:", m.pipeline_reg_bits)
print("OK - no AttributeError")
