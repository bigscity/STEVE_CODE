import math
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from lib.utils import masked_mae_loss
from models.module import ST_encoder,CLUB,Configs
from models.other_models.AGCRN import AGCRN
from models.layers import RevGradLayer,ScaledDotProductAttention,pca_whitening,MLPAttention


class StableST(nn.Module):
    def __init__(
            self,
            args,
            adj,
            in_channels=1,
            embed_size=64,
            T_dim=12,
            output_T_dim=1,
            output_dim=2,
            device="cuda"
    ):
        super(StableST, self).__init__()

        self.args = args
        self.adj = adj

        self.time_labels = 48
        self.mi_w=args.mi_w

        self.embed_size = embed_size

        
        T_dim = args.input_length-4*(3-1)
        self.K = int(args.d_model*args.kw)
        # T_dim = 11

        temp_spatial_label=list(range(args.num_nodes))

        self.spatial_label=torch.tensor(temp_spatial_label,device=args.device)

        # STGCN encoder
        self.st_encoder4variant = ST_encoder(args.num_nodes, args.d_input, args.d_model, 3, 3,
                               [[args.d_model, args.d_model // 2, args.d_model],
                                [args.d_model, args.d_model // 2, args.d_model]], args.input_length, args.dropout,
                               args.device)

        self.st_encoder4invariant = ST_encoder(args.num_nodes, args.d_input, args.d_model, 3, 3,
                        [[args.d_model, args.d_model // 2, args.d_model],
                        [args.d_model, args.d_model // 2, args.d_model]], args.input_length, args.dropout,
                        args.device)

        # AGCRN
        # config=Configs(vars(args))
        # self.st_encoder4variant = AGCRN(config)
        # self.st_encoder4invariant = AGCRN(config) 

        # dynamic adj metric
        self.node_embeddings_1 = nn.Parameter(torch.randn(3, args.num_nodes, embed_size), requires_grad=True)
        self.node_embeddings_2 = nn.Parameter(torch.randn(3, embed_size, args.num_nodes), requires_grad=True)


        # predict

        self.tcl4c = nn.Conv2d(T_dim, output_T_dim, 1,bias=True)

        self.tcl4h = nn.Conv2d(T_dim, output_T_dim, 1,bias=True)

        self.variant_predict_conv_1 = nn.Conv2d(T_dim, output_T_dim, 1)
        
        self.variant_predict_conv_2 = nn.Conv2d(embed_size, output_dim, 1)

        self.invariant_predict_conv_1 = nn.Conv2d(T_dim, output_T_dim, 1)
        
        self.invariant_predict_conv_2 = nn.Conv2d(embed_size, output_dim, 1)

        self.relu = nn.ReLU()

        #variant 

        self.variant_tconv = nn.Conv2d(in_channels=T_dim,
                                       out_channels=1,
                                       kernel_size=(1, 1),
                                       bias=True)
        self.variant_end_temproal = nn.Sequential(
            nn.Linear(embed_size, embed_size * 2),
            nn.ReLU(),
            nn.Linear(embed_size * 2, self.time_labels)
        )
        self.variant_end_spacial = nn.Sequential(
            nn.Linear(embed_size, embed_size * 2),
            nn.ReLU(),
            nn.Linear(embed_size * 2, args.num_nodes)
        )

        self.variant_end_congest = nn.Sequential(
            nn.Linear(embed_size, embed_size // 2),
            nn.ReLU(),
            nn.Linear(embed_size // 2 , 2)
        )

        # invariant
        self.invariant_tconv = nn.Conv2d(in_channels=T_dim,
                                       out_channels=1,
                                       kernel_size=(1, 1),
                                       bias=True)
        self.invariant_end_temporal = nn.Sequential(
            nn.Linear(embed_size, embed_size * 2),
            nn.ReLU(),
            nn.Linear(embed_size*2, self.time_labels)
        )
        self.invariant_end_spatial = nn.Sequential(
            nn.Linear(embed_size, embed_size * 2),
            nn.ReLU(),
            nn.Linear(embed_size*2, args.num_nodes)
        )

        self.invariant_end_congest = nn.Sequential(
            nn.Linear(embed_size, embed_size // 2),
            nn.ReLU(),
            nn.Linear(embed_size // 2 , 2)
        )

        self.alpha_linear=nn.Linear(2, 2)
        self.beta_linear=nn.Linear(2, 2)
        # self.alpha=nn.Parameter(torch.rand(1,args.num_nodes,2))
        
        self.revgrad=RevGradLayer()
        # for ablation
        # self.revgrad=nn.Parameter(torch.tensor(1.,dtype=torch.float),requires_grad=False)

        self.mask=torch.zeros([args.batch_size,args.d_input,args.input_length,args.num_nodes],dtype=torch.float).to(device)
        self.receptive_field = args.input_length + 8

        self.mse_loss=torch.nn.MSELoss()

        self.mi_net=CLUB(embed_size,embed_size,embed_size*self.mi_w) # regularizer # BJTaxi for 4, other goes 2

        self.optimizer_mi_net=torch.optim.Adam(self.mi_net.parameters(),lr=0.1)

        self.mae = masked_mae_loss(mask_value=5.0)

        self.generator_conv=nn.Conv2d(in_channels=args.input_length,
                                       out_channels=1,
                                       kernel_size=(1, 1),
                                       bias=True)
        
        bank_temp=np.random.randn(self.K,self.embed_size)
        bank_temp=pca_whitening(bank_temp)
        
        self.Bank=nn.Parameter(torch.tensor(bank_temp,dtype=torch.float),requires_grad=False)
        self.mlp4bank=nn.Linear(T_dim*args.num_nodes,self.K)
        self.att4bank=MLPAttention(self.embed_size)
        self.bank_gamma=args.bank_gamma
        self.W_weight=nn.Parameter(torch.randn(embed_size,2),requires_grad=True)

 
        self.mlp4C = nn.Sequential(
            nn.Linear(embed_size,embed_size//2),
            nn.ReLU(),
            nn.Linear(embed_size//2,2),
            #nn.Sigmoid()
        )

        self.mlp4H = nn.Sequential(
            nn.Linear(embed_size,embed_size//2),
            nn.ReLU(),
            nn.Linear(embed_size//2,2),
            #nn.Sigmoid()
        )

        self.reset_parameters()
        # new
        # self.linear_b = nn.Sequential(
        #     nn.Linear(2,embed_size),
        #     nn.ReLU(),
        #     nn.Linear(embed_size,2),
        #     nn.Sigmoid()
        # )
        
        # self.linear_a = nn.Sequential(
        #     nn.Linear(2,embed_size),
        #     nn.ReLU(),
        #     nn.Linear(embed_size,2),
        #     nn.Sigmoid()
        # )

    def reset_parameters(self):
        # 初始化方法
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)    # Xavier 初始化确保权重“恰到好处”
            else:
                nn.init.uniform_(p)



    def forward(self, x,adj=None):
        # batch,seqs,dim,_,_=x.shape
        # x = x.reshape(batch,seqs,dim,-1)
        # x = x.permute(0,1,3,2)
        x = x.permute(0, 3, 1, 2) #NCLV STGCN
        # fix me
        # encoder_inputs = self.conv1(x)
        if adj == None:
            invariant_output = self.st_encoder4invariant(x,self.adj)
        else :
            invariant_output = self.st_encoder4invariant(x,adj)
        H_tensor = invariant_output.permute(0, 2, 3, 1) #STGCN

        # invariant_output = self.st_encoder4invariant(x)#AGCRN

        adaptive_adj = F.softmax(F.relu(torch.bmm(self.node_embeddings_1, self.node_embeddings_2)), dim=1)
        # if adj == None: #todo
        #     variant_output = self.st_encoder4variant(x,self.adj)
        # else:
        variant_output = self.st_encoder4variant.variant_encode(x,adaptive_adj)
        # variant_output = self.st_encoder4variant(x,self.adj)
        Z_tensor = variant_output.permute(0, 2, 3, 1)

        # variant_output = self.st_encoder4variant(x)#AGCRN
    

        # return out shape: [v, output_dim]->[B,V,output_dim]
        return H_tensor,Z_tensor
        
    def predict(self, Z_tensor, C_tensor, H):
        C_tensor=C_tensor.unsqueeze(1)# todo
        out=C_tensor+self.tcl4c(Z_tensor)
        out=out.permute(0,3,2,1)
        Y_c=self.variant_predict_conv_2(out)
        Y_c=Y_c.permute(0,3,2,1)
        H=H.permute(0,3,2,1)
        Y_h=self.invariant_predict_conv_2(H) # b,1,n,2
        Y_h=Y_h.permute(0,3,2,1)

        C_weight=torch.relu(torch.matmul(C_tensor,self.W_weight))# todo

        Y=C_weight*Y_c+Y_h# todo
        # Y=Y_c+Y_h
        return Y
    # def predict(self, z1, z2):
    #     out_1 = self.relu(self.invariant_predict_conv_1(z1))  # out shape: [1, output_T_dim, N, C]
    #     out_1 = out_1.permute(0, 3, 2, 1)  # out shape: [1, C, N, output_T_dim] 64 128 64 1
    #     out_1 = self.invariant_predict_conv_2(out_1)  # out shape: [b, 1, N, output_T_dim]
    #     out_1=out_1.permute(0,3,2,1)

    #     out_2 = self.relu(self.variant_predict_conv_1(z2))  # out shape: [1, output_T_dim, N, C]
    #     out_2 = out_2.permute(0, 3, 2, 1)  # out shape: [1, C, N, output_T_dim]
    #     out_2 = self.variant_predict_conv_2(out_2)  # out shape: [b, c, N, t]
    #     out_2 = out_2.permute(0,3,2,1)

    #     # todolist change
    #     # x=x.permute(0,2,3,1)#nclv
    #     # c=self.generator_conv(x) # b，1，n，2
    #     # out_c = self.linear_c(c)
    #     # out_2 = out_2*out_c
        
    #     # self.out_c=out_c

    #     # out =torch.cat([out_1,out_2],dim=1).permute(0,2,3,1)
    #     # out = self.out_mlp(out).squeeze()
    #     out = out_2 + out_1
    #     out = out.squeeze(1)
    #     out_1=out_1.squeeze(1)
    #     out_2=out_2.squeeze(1)
    #     return out
    
    def predict_test(self, Z_tensor, H_tensor):
        
        H=self.tcl4h(H_tensor) # b,n,c
        C_tensor,att=self.confounder_ext(Z_tensor,train=False)# todo

        C_tensor=C_tensor.unsqueeze(1)# todo
        out=C_tensor+self.tcl4c(Z_tensor)
        out=out.permute(0,3,2,1)
        Y_c=self.variant_predict_conv_2(out)
        Y_c=Y_c.permute(0,3,2,1)
        H=H.permute(0,3,2,1)
        Y_h=self.invariant_predict_conv_2(H) # b,1,n,2
        Y_h=Y_h.permute(0,3,2,1)

        C_weight=torch.relu(torch.matmul(C_tensor,self.W_weight))# todo

        Y=C_weight*Y_c+Y_h# todo
        # Y=Y_c+Y_h
        return Y,att,C_tensor, H

    

    def confounder_ext(self, Z_tensor,train=True):
        """
        Z_tensor: shape=[b,t,n,c]
        return: 
            C_tensor:shape=[b,n,d]
        """
        b,t,n,c=Z_tensor.shape
        Z_tilda=Z_tensor.reshape(b,n*t,c)
        Z_tilda=Z_tilda.permute(0,2,1)

        B_tilda=self.mlp4bank(Z_tilda) # shape = b,d,k
        B_tilda=B_tilda.permute(0,2,1) # shape = b,k,d


        B_new=[]
        for i in range(b):
            _B_new=self.bank_gamma*self.Bank+(1-self.bank_gamma)*B_tilda[i] # b,k,d
            self.Bank.set_(_B_new.detach())
            B_new.append(_B_new)

        B_new=torch.stack(B_new)


        # if train:
        # self.Bank.set_(B_new.mean(0).detach())
        # print(self.Bank)

        Q=Z_tensor.mean(1) # shape = b,n,d
        C_tensor,att = self.att4bank(Q,B_new,B_new)
        
        if self.args.ablation == 'bank':
            C_tensor=Z_tensor.mean(1)
        
        # print(C_tensor)

        return C_tensor,att


    def variant_loss(self, C_tensor, date, c):
        # C_tensor.shape=[b,n,c]
        # date = [b,time_intervals]
        # c = [b,n,1]
        z_temporal = C_tensor.mean(1).squeeze() # b,c
        
        y_temporal = self.variant_end_temproal(z_temporal)  # b,time_num
        loss_temporal = F.cross_entropy(y_temporal, date)

        y_spatial = self.variant_end_spacial(C_tensor) # b,n,n
        y_spatial = y_spatial.mean(0)
        loss_spatial = F.cross_entropy(y_spatial,self.spatial_label)

        y_congest = C_tensor.unsqueeze(-1)
        y_congest = self.variant_end_congest(C_tensor) # b,n,2,1
        loss_congest = self.mse_loss(y_congest,c)
        
        
        if self.args.ablation == 'spatial':
            # print("spatial")
            loss=(loss_congest+loss_temporal)/2.
        elif self.args.ablation == 'temporal':
            # print("temporal")
            loss=(loss_congest+loss_spatial)/2.
        elif self.args.ablation == 'traffic':
            # print("traffic")
            loss=(loss_temporal+loss_spatial)/2.
        else:
           loss = (loss_spatial+loss_temporal+loss_congest)/3.
        # loss = (loss_spatial+loss_temporal+loss_congest)/3.   
        # loss =  loss_temporal

        # loss = (loss_spatial+loss_temporal)/2.0
        return loss
    

    def invariant_loss(self, H, date,c,p=None,training=True):
        # z1.shape=[b,t,c,n]
        # recover_loss
        #revgrad loss
        # mask for ablation
        # z1_r = self.revgrad(z1)
        z1_r=H
        if training==True and self.args.ablation!='gr':
            z1_r=self.revgrad(H, p)

        z1_r = z1_r.squeeze(1)  # [b,n,c]
        z1_temporal = z1_r.mean(1).squeeze() 
        
        y_temporal = self.invariant_end_temporal(z1_temporal)  # b,time_num
        loss_temporal = F.cross_entropy(y_temporal, date) 

        y_spatial = self.invariant_end_spatial(z1_r)# b,num_nodes
        y_spatial = y_spatial.mean(0)# num_nodes
        loss_spatial = F.cross_entropy(y_spatial,self.spatial_label)


        z1_congest = z1_r.unsqueeze(1)
        y_congest = self.invariant_end_congest(z1_congest)
        loss_congest = self.mse_loss(y_congest,c)
        
        if self.args.ablation == 'spatial':
            # print("spatial")
            loss=(loss_congest+loss_temporal)/2.
        elif self.args.ablation == 'temporal':
            # print("temporal")
            loss=(loss_congest+loss_spatial)/2.
        elif self.args.ablation == 'traffic':
            # print("traffic")
            loss=(loss_temporal+loss_spatial)/2.
        else:
            loss = (loss_spatial+loss_temporal+loss_congest)/3.
        # loss = (loss_spatial+loss_temporal)/2.0
        return loss # shape=[]

    def pred_loss(self, Z_tensor, C_tensor, H, y_true, scaler):
        y_pred = self.predict(Z_tensor, C_tensor, H)# todo
        # y_pred = self.predict(Z_tensor, C_tensor)
        
        y_pred = scaler.inverse_transform(y_pred)
        y_true = scaler.inverse_transform(y_true)
        # y_true = y_true.squeeze(1)

        loss = self.args.yita * self.mae(y_pred[..., 0], y_true[..., 0])
        loss += (1 - self.args.yita) * self.mae(y_pred[..., 1], y_true[..., 1])
        return loss #[1]


    def calculate_loss(self, Z_tensor, H_tensor, target, c, time_label,scaler,loss_weights,p=None,training=False):
        # z1.shape=btcv
        H=self.tcl4h(H_tensor) # b,n,c
        C_tensor,att=self.confounder_ext(Z_tensor) # b,n,c # todo
        # C_tensor=self.tcl4c(Z_tensor).squeeze(1) # b,1,n,c
                
        lp = self.pred_loss(Z_tensor, C_tensor, H, target, scaler)
        loss=0 
        lm=0

        sep_loss = [lp.item()]

        # mi_net train
        if training and self.args.ablation!='idp':
             z1_temp=H.squeeze(1).reshape(-1,H.shape[-1]) # nb,c
             z2_temp=C_tensor.reshape(-1,H.shape[-1])# nb,c
             self.mi_net.train()
             all_len=z1_temp.shape[0]
             random_choice=np.random.choice(all_len,int(all_len*0.1))
             temp1=z1_temp[random_choice].detach()
             temp2=z2_temp[random_choice].detach()
             for i in range(5):
                 self.optimizer_mi_net.zero_grad()
                 mi_loss=self.mi_net.learning_loss(temp1,temp2)
                 mi_loss.backward()
                 self.optimizer_mi_net.step()
             self.mi_net.eval()
            
             lm = self.mi_net(z1_temp,z2_temp)
             loss += 0.1*lm

        loss += loss_weights[0] * lp

        
        lc = self.variant_loss(C_tensor, time_label, c)
        sep_loss.append(lc.item())
        if self.args.ablation != 'cd':
            loss += loss_weights[1] * lc

        
        lh = self.invariant_loss(H, time_label ,c, p,training)
        sep_loss.append(lh.item())
        if self.args.ablation != 'cd':
            loss += loss_weights[2] * lh

        if training == False :
            if self.args.lr_mode=='only':
                loss = lp
            elif self.args.lr_mode=='add':
                loss = lp+lc

        return loss, sep_loss,lm
