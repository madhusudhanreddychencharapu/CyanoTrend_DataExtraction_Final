"""Frozen independently extracted notebook AST contracts, not self-generated expectations."""
import ast
import hashlib
import json
from pathlib import Path

class Normalize(ast.NodeTransformer):
    def visit_Attribute(self,node):
        self.generic_visit(node)
        if isinstance(node.value,ast.Name) and node.value.id.startswith('_m_'):
            return ast.copy_location(ast.Name(id=node.attr,ctx=node.ctx),node)
        return node
    def visit_FunctionDef(self,node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and isinstance(node.body[0].value.value,str):
            node.body=node.body[1:]
        return node

def test_notebook_function_bodies_remain_equivalent():
    root=Path(__file__).resolve().parents[1]
    contracts=json.loads((root/'docs/notebook_function_contract.json').read_text())
    assert len(contracts)>=50
    for contract in contracts:
        tree=ast.parse((root/'src/cyanotrend/core'/f"{contract['module']}.py").read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==contract['function'])
        normalized=ast.dump(Normalize().visit(node),include_attributes=False).replace(", type_params=[]", "")
        assert hashlib.sha256(normalized.encode()).hexdigest()==contract['sha256'],contract

def test_supplied_colab_configuration_hash():
    from cyanotrend.core.configuration import processing_config_hash
    # Independent expected value from the user's Colab scene metadata.
    assert processing_config_hash(1.3, 'USA') == '58f41b2c1318eaf1'
    assert processing_config_hash(1.3, 'WORLD') != '58f41b2c1318eaf1'
