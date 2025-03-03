import torch
import torch.nn as nn
import torch.nn.functional as F

import dgl
import dgl.function as fn
import numpy as np

from model.modules.efficient_kan import make_kans

"""
    Graph Transformer Layer
    
"""

"""
    Util functions
"""
def src_dot_dst(src_field, dst_field, out_field):
    def func(edges):
        return {out_field: (edges.src[src_field] * edges.dst[dst_field])}
    return func


def scaling(field, scale_constant):
    def func(edges):
        return {field: ((edges.data[field]) / scale_constant)}
    return func

# Improving implicit attention scores with explicit edge features, if available
def imp_exp_attn(implicit_attn, explicit_edge):
    """
        implicit_attn: the output of K Q
        explicit_edge: the explicit edge features
    """
    def func(edges):
        return {implicit_attn: (edges.data[implicit_attn] * edges.data[explicit_edge])}
    return func


def exp_real(field, L):
    def func(edges):
        # clamp for softmax numerical stability
        return {'score_soft': torch.exp((edges.data[field].sum(-1, keepdim=True)).clamp(-5, 5))/(L+1)}
    return func


def exp_fake(field, L):
    def func(edges):
        # clamp for softmax numerical stability
        return {'score_soft': L*torch.exp((edges.data[field].sum(-1, keepdim=True)).clamp(-5, 5))/(L+1)}
    return func

def exp(field):
    def func(edges):
        # clamp for softmax numerical stability
        return {'score_soft': torch.exp((edges.data[field].sum(-1, keepdim=True)).clamp(-5, 5))}
    return func


"""
    Single Attention Head
"""

class MultiHeadAttentionLayer(nn.Module):
    def __init__(self, gamma, in_dim, out_dim, num_heads, full_graph, use_bias):
        super().__init__()
        
       
        self.out_dim = out_dim
        self.num_heads = num_heads
        self.gamma = gamma
        self.full_graph=full_graph
        
        if use_bias:
            self.Q = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            self.K = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            self.E = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
            if self.full_graph:
                self.Q_2 = nn.Linear(in_dim, out_dim * num_heads, bias=True)
                self.K_2 = nn.Linear(in_dim, out_dim * num_heads, bias=True)
                self.E_2 = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
            self.V = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
        else:
            self.Q = nn.Linear(in_dim, out_dim * num_heads, bias=False)
            self.K = nn.Linear(in_dim, out_dim * num_heads, bias=False)
            self.E = nn.Linear(in_dim, out_dim * num_heads, bias=False)
            
            if self.full_graph:
                self.Q_2 = nn.Linear(in_dim, out_dim * num_heads, bias=False)
                self.K_2 = nn.Linear(in_dim, out_dim * num_heads, bias=False)
                self.E_2 = nn.Linear(in_dim, out_dim * num_heads, bias=False)
                
            self.V = nn.Linear(in_dim, out_dim * num_heads, bias=False)
    
    def propagate_attention(self, g):

        
        if self.full_graph:
            real_ids = torch.nonzero(g.edata['real']).squeeze()
            fake_ids = torch.nonzero(g.edata['real']==0).squeeze()

        else:
            real_ids = g.edges(form='eid')
            
        g.apply_edges(src_dot_dst('K_h', 'Q_h', 'score'), edges=real_ids)
        
        if self.full_graph:
            g.apply_edges(src_dot_dst('K_2h', 'Q_2h', 'score'), edges=fake_ids)
        

        # scale scores by sqrt(d)
        g.apply_edges(scaling('score', np.sqrt(self.out_dim)))
        
        # Use available edge features to modify the scores for edges
        # g.apply_edges(imp_exp_attn('score', 'E'), edges=real_ids)
        
        if self.full_graph:
            g.apply_edges(imp_exp_attn('score', 'E_2'), edges=fake_ids)
    
        if self.full_graph:
            # softmax and scaling by gamma
            L=self.gamma
            g.apply_edges(exp_real('score', L), edges=real_ids)
            g.apply_edges(exp_fake('score', L), edges=fake_ids)
        
        else:
            g.apply_edges(exp('score'), edges=real_ids)

        # Send weighted values to target nodes
        eids = g.edges()
        g.send_and_recv(eids, fn.u_mul_e('V_h', 'score_soft', 'V_h'), fn.sum('V_h', 'wV'))
        g.send_and_recv(eids, fn.copy_e('score_soft', 'score_soft'), fn.sum('score_soft', 'z'))
    
    
    def forward(self, g, h):
        
        Q_h = self.Q(h)
        K_h = self.K(h)
        # E = self.E(e)
        
        if self.full_graph:
            Q_2h = self.Q_2(h)
            K_2h = self.K_2(h)
            # E_2 = self.E_2(e)
            
        V_h = self.V(h)

        
        # Reshaping into [num_nodes, num_heads, feat_dim] to 
        # get projections for multi-head attention
        g.ndata['Q_h'] = Q_h.view(-1, self.num_heads, self.out_dim)
        g.ndata['K_h'] = K_h.view(-1, self.num_heads, self.out_dim)
        # g.edata['E'] = E.view(-1, self.num_heads, self.out_dim)
        
        
        if self.full_graph:
            g.ndata['Q_2h'] = Q_2h.view(-1, self.num_heads, self.out_dim)
            g.ndata['K_2h'] = K_2h.view(-1, self.num_heads, self.out_dim)
            # g.edata['E_2'] = E_2.view(-1, self.num_heads, self.out_dim)
        
        g.ndata['V_h'] = V_h.view(-1, self.num_heads, self.out_dim)

        self.propagate_attention(g)
        
        h_out = g.ndata['wV'] / (g.ndata['z'] + torch.full_like(g.ndata['z'], 1e-6))
        
        return h_out

class MultiHeadAttentionLayer_kan(nn.Module):
    def __init__(self, gamma, in_dim, out_dim, num_heads, full_graph, use_bias, spline_order, grid_size, hidden_layers):
        super().__init__()
        
       
        self.out_dim = out_dim
        self.num_heads = num_heads
        self.gamma = gamma
        self.full_graph=full_graph
        
        if use_bias:
            self.Q = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            self.K = make_kans(in_dim, out_dim, out_dim, num_heads, hidden_layers, grid_size, spline_order)
            self.E = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
            if self.full_graph:
                self.Q_2 = nn.Linear(in_dim, out_dim * num_heads, bias=True)
                self.K = make_kans(in_dim, out_dim, out_dim, num_heads, hidden_layers, grid_size, spline_order)
                self.E_2 = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
            self.V = nn.Linear(in_dim, out_dim * num_heads, bias=True)
            
        else:
            self.Q = nn.Linear(in_dim, out_dim * num_heads, bias=False)
            self.K = make_kans(in_dim, out_dim, out_dim, num_heads, hidden_layers, grid_size, spline_order)
            self.E = nn.Linear(in_dim, out_dim * num_heads, bias=False)
            
            if self.full_graph:
                self.Q_2 = nn.Linear(in_dim, out_dim * num_heads, bias=False)
                self.K = make_kans(in_dim, out_dim, out_dim, num_heads, hidden_layers, grid_size, spline_order)
                self.E_2 = nn.Linear(in_dim, out_dim * num_heads, bias=False)
                
            self.V = nn.Linear(in_dim, out_dim * num_heads, bias=False)
    
    def propagate_attention(self, g):

        
        if self.full_graph:
            real_ids = torch.nonzero(g.edata['real']).squeeze()
            fake_ids = torch.nonzero(g.edata['real']==0).squeeze()

        else:
            real_ids = g.edges(form='eid')
            
        g.apply_edges(src_dot_dst('K_h', 'Q_h', 'score'), edges=real_ids)
        
        if self.full_graph:
            g.apply_edges(src_dot_dst('K_2h', 'Q_2h', 'score'), edges=fake_ids)
        

        # scale scores by sqrt(d)
        g.apply_edges(scaling('score', np.sqrt(self.out_dim)))
        
        # Use available edge features to modify the scores for edges
        # g.apply_edges(imp_exp_attn('score', 'E'), edges=real_ids)
        
        if self.full_graph:
            g.apply_edges(imp_exp_attn('score', 'E_2'), edges=fake_ids)
    
        if self.full_graph:
            # softmax and scaling by gamma
            L=self.gamma
            g.apply_edges(exp_real('score', L), edges=real_ids)
            g.apply_edges(exp_fake('score', L), edges=fake_ids)
        
        else:
            g.apply_edges(exp('score'), edges=real_ids)

        # Send weighted values to target nodes
        eids = g.edges()
        g.send_and_recv(eids, fn.u_mul_e('V_h', 'score_soft', 'V_h'), fn.sum('V_h', 'wV'))
        g.send_and_recv(eids, fn.copy_e('score_soft', 'score_soft'), fn.sum('score_soft', 'z'))
    
    
    def forward(self, g, h):
        
        Q_h = self.Q(h)
        h_kan = h.unsqueeze(1).expand(-1, self.num_heads, -1)
        K_h = self.K(h_kan)
        # E = self.E(e)
        
        if self.full_graph:
            Q_2h = self.Q_2(h)
            K_2h = self.K_2(h_kan)
            # E_2 = self.E_2(e)
            
        V_h = self.V(h)

        
        # Reshaping into [num_nodes, num_heads, feat_dim] to 
        # get projections for multi-head attention
        g.ndata['Q_h'] = Q_h.view(-1, self.num_heads, self.out_dim)
        g.ndata['K_h'] = K_h.view(-1, self.num_heads, self.out_dim)
        # g.edata['E'] = E.view(-1, self.num_heads, self.out_dim)
        
        
        if self.full_graph:
            g.ndata['Q_2h'] = Q_2h.view(-1, self.num_heads, self.out_dim)
            g.ndata['K_2h'] = K_2h.view(-1, self.num_heads, self.out_dim)
            # g.edata['E_2'] = E_2.view(-1, self.num_heads, self.out_dim)
        
        g.ndata['V_h'] = V_h.view(-1, self.num_heads, self.out_dim)

        self.propagate_attention(g)
        
        h_out = g.ndata['wV'] / (g.ndata['z'] + torch.full_like(g.ndata['z'], 1e-6))
        
        return h_out
    

class GraphTransformerLayer(nn.Module):
    """
        Param: 
    """
    def __init__(self, kind, gamma, in_dim, out_dim, num_heads, full_graph, spline_order, grid_size, hidden_layers, dropout=0.0, layer_norm=False, batch_norm=True, residual=True, use_bias=False):
        super().__init__()
        
        self.in_channels = in_dim
        self.out_channels = out_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.residual = residual
        self.layer_norm = layer_norm     
        self.batch_norm = batch_norm
        
        if kind == 'KAA_SAN':
            self.attention = MultiHeadAttentionLayer_kan(gamma, in_dim, out_dim//num_heads, num_heads, full_graph, use_bias, spline_order, grid_size, hidden_layers)
        else:
            self.attention = MultiHeadAttentionLayer(gamma, in_dim, out_dim//num_heads, num_heads, full_graph, use_bias)

        self.O_h = nn.Linear(out_dim, out_dim)

        if self.layer_norm:
            self.layer_norm1_h = nn.LayerNorm(out_dim)
            
        if self.batch_norm:
            self.batch_norm1_h = nn.BatchNorm1d(out_dim)
        
        # FFN for h
        self.FFN_h_layer1 = nn.Linear(out_dim, out_dim*2)
        self.FFN_h_layer2 = nn.Linear(out_dim*2, out_dim)
        

        if self.layer_norm:
            self.layer_norm2_h = nn.LayerNorm(out_dim)
            
        if self.batch_norm:
            self.batch_norm2_h = nn.BatchNorm1d(out_dim)
        
    def forward(self, g, h):
        h_in1 = h # for first residual connection
        
        # multi-head attention out
        h_attn_out = self.attention(g, h)
        
        #Concat multi-head outputs
        h = h_attn_out.view(-1, self.out_channels)
       
        h = F.dropout(h, self.dropout, training=self.training)

        h = self.O_h(h)

        if self.residual:
            h = h_in1 + h # residual connection

        if self.layer_norm:
            h = self.layer_norm1_h(h)

        if self.batch_norm:
            h = self.batch_norm1_h(h)

        h_in2 = h # for second residual connection

        # FFN for h
        h = self.FFN_h_layer1(h)
        h = F.relu(h)
        h = F.dropout(h, self.dropout, training=self.training)
        h = self.FFN_h_layer2(h)

        if self.residual:
            h = h_in2 + h # residual connection       

        if self.layer_norm:
            h = self.layer_norm2_h(h)

        if self.batch_norm:
            h = self.batch_norm2_h(h)         

        # return h, e
        return h
        
    def __repr__(self):
        return '{}(in_channels={}, out_channels={}, heads={}, residual={})'.format(self.__class__.__name__,
                                             self.in_channels,
                                             self.out_channels, self.num_heads, self.residual)