import torch
import torch.nn as nn

"""
    Graph Transformer
    
"""
from model.SAN_KAN.graph_transformer_layer import GraphTransformerLayer
from model.modules.mlp_readout_layer import MLPReadout

class SAN_NodeLPE(nn.Module):

    def __init__(self, net_params):
        super().__init__()

        in_dim_node = net_params['in_dim']
        self.n_classes = net_params['n_classes']
        
        full_graph = net_params['full_graph']
        gamma = net_params['gamma']
        
        LPE_layers = net_params['LPE_layers']
        LPE_dim = net_params['LPE_dim']
        LPE_n_heads = net_params['LPE_n_heads']
        
        GT_layers = net_params['GT_layers']
        GT_hidden_dim = net_params['GT_hidden_dim']
        GT_out_dim = net_params['GT_out_dim']
        GT_n_heads = net_params['GT_n_heads']
        
        self.residual = net_params['residual']
        # self.readout = net_params['readout']
        in_feat_dropout = net_params['in_feat_dropout']
        dropout = net_params['dropout']
        kind = net_params['kind']

        # self.readout = net_params['readout']
        self.layer_norm = net_params['layer_norm']
        self.batch_norm = net_params['batch_norm']

        spline_order = net_params['spline_order']
        grid_size = net_params['grid_size']
        hidden_layers = net_params['hidden_layers']

        self.device = net_params['device']
        self.in_feat_dropout = nn.Dropout(in_feat_dropout)
        
        self.embedding_h = nn.Linear(in_dim_node, GT_hidden_dim-LPE_dim)#Remove some embedding dimensions to make room for concatenating laplace encoding
        self.embedding_e = nn.Embedding(2, GT_hidden_dim)
        self.linear_A = nn.Linear(2, LPE_dim)
        
        encoder_layer = nn.TransformerEncoderLayer(d_model=LPE_dim, nhead=LPE_n_heads)
        self.PE_Transformer = nn.TransformerEncoder(encoder_layer, num_layers=LPE_layers)
        
        self.layers = nn.ModuleList([ GraphTransformerLayer(kind, gamma, GT_hidden_dim, GT_hidden_dim, GT_n_heads, full_graph, spline_order, grid_size, hidden_layers, dropout, self.layer_norm, self.batch_norm, self.residual) for _ in range(GT_layers-1) ])
        
        self.layers.append(GraphTransformerLayer(kind, gamma, GT_hidden_dim, GT_out_dim, GT_n_heads, full_graph, spline_order, grid_size, hidden_layers, dropout, self.layer_norm, self.batch_norm, self.residual))

        self.MLP_layer = MLPReadout(GT_out_dim, self.n_classes)


    def forward(self, g, h, EigVecs, EigVals):
        
        # input embedding
        h = self.embedding_h(h)
        # e = self.embedding_e(e) 
          
        PosEnc = torch.cat((EigVecs.unsqueeze(2), EigVals), dim=2).float() # (Num nodes) x (Num Eigenvectors) x 2
        empty_mask = torch.isnan(PosEnc) # (Num nodes) x (Num Eigenvectors) x 2
        
        PosEnc[empty_mask] = 0 # (Num nodes) x (Num Eigenvectors) x 2
        PosEnc = torch.transpose(PosEnc, 0 ,1).float() # (Num Eigenvectors) x (Num nodes) x 2
        PosEnc = self.linear_A(PosEnc) # (Num Eigenvectors) x (Num nodes) x PE_dim
        
        
        #1st Transformer: Learned PE
        PosEnc = self.PE_Transformer(src=PosEnc, src_key_padding_mask=empty_mask[:,:,0]) 
        
        #remove masked sequences
        PosEnc[torch.transpose(empty_mask, 0 ,1)[:,:,0]] = float('nan') 
        
        #Sum pooling
        PosEnc = torch.nansum(PosEnc, 0, keepdim=False)
        
        #Concatenate learned PE to input embedding
        h = torch.cat((h, PosEnc), 1)
        
        h = self.in_feat_dropout(h)
        
        # GraphTransformer Layers
        for conv in self.layers:
            h = conv(g, h)
            
        # output
        h_out = self.MLP_layer(h)

        return h_out
    
    
    def loss(self, pred, label):

        # calculating label weights for weighted loss computation
        V = label.size(0)
        label_count = torch.bincount(label)
        label_count = label_count[label_count.nonzero()].squeeze()
        cluster_sizes = torch.zeros(self.n_classes).long().to(self.device)
        cluster_sizes[torch.unique(label)] = label_count
        weight = (V - cluster_sizes).float() / V
        weight *= (cluster_sizes>0).float()
        
        # weighted cross-entropy for unbalanced classes
        criterion = nn.CrossEntropyLoss(weight=weight)
        loss = criterion(pred, label)

        return loss



        