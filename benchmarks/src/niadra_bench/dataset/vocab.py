"""Synthetic vocabulary: five business domains in Portuguese and English. No real person, company or
document: names are common first and last names combined at random, companies are invented, and every
number is drawn by the generator.
"""

from __future__ import annotations

from typing import TypedDict


class Domain(TypedDict):
    company: dict[str, str]
    object_type: str
    object_ns: str
    item: dict[str, str]
    issue: dict[str, list[str]]
    issue_about: dict[str, str]
    recurring: dict[str, list[str]]
    deadline_reason: dict[str, list[str]]
    milestone_type: str
    milestone: dict[str, str]
    milestone_after: dict[str, str]
    after_issue: dict[str, list[str]]
    before_issue: dict[str, list[str]]
    promise_op: str
    confirm_type: str
    promise: dict[str, str]
    promise_noun: dict[str, str]
    confirm_label: dict[str, str]
    sensitive_kind: str
    sensitive_say: dict[str, str]
    sensitive_ask: dict[str, str]
    address_label: dict[str, str]
    fillers: dict[str, list[tuple[str, str]]]


DOMAINS: dict[str, Domain] = {
    "telecom": {
        "company": {"pt": "Conecta Telecom", "en": "Beacon Telecom"},
        "object_type": "ticket",
        "object_ns": "crm",
        "item": {"pt": "o chamado", "en": "ticket"},
        "issue": {
            "pt": ["a internet está caindo toda noite", "a fibra fica sem sinal depois das oito"],
            "en": ["the internet keeps dropping every night", "the fiber loses signal after eight pm"],
        },
        "issue_about": {"pt": "da internet caindo", "en": "about the internet dropping"},
        "recurring": {
            "pt": [
                "A internet caiu de novo ontem à noite.",
                "Mais uma vez fiquei sem internet à noite.",
                "A conexão voltou a cair, igual antes.",
                "De novo a internet caindo no meio da noite.",
            ],
            "en": [
                "The internet dropped again last night.",
                "Once more I lost internet in the evening.",
                "The connection went down again, same as before.",
                "Again the internet dropping in the middle of the night.",
            ],
        },
        "deadline_reason": {
            "pt": ["trabalho de casa e tenho uma apresentação", "minha filha tem prova online"],
            "en": ["I work from home and have a client presentation", "my daughter has an online exam"],
        },
        "milestone_type": "field_visit.completed",
        "milestone": {"pt": "Visita técnica concluída", "en": "Technician visit completed"},
        "milestone_after": {"pt": "da visita do técnico", "en": "after the technician's visit"},
        "after_issue": {
            "pt": ["o modem começou a reiniciar sozinho", "o telefone fixo ficou mudo"],
            "en": ["the modem started restarting by itself", "the landline went silent"],
        },
        "before_issue": {
            "pt": ["a velocidade estava abaixo do contratado", "o roteador piscava vermelho"],
            "en": ["the speed was below the plan", "the router was blinking red"],
        },
        "promise_op": "credit",
        "confirm_type": "invoice.credited",
        "promise": {
            "pt": "um crédito de {amount} na próxima fatura",
            "en": "a credit of {amount} on your next bill",
        },
        "promise_noun": {"pt": "crédito", "en": "credit"},
        "confirm_label": {"pt": "nota de crédito", "en": "credit note"},
        "sensitive_kind": "document",
        "sensitive_say": {"pt": "O número do meu documento é {value}.", "en": "My ID number is {value}."},
        "sensitive_ask": {
            "pt": "qual é o número de documento que está no meu cadastro",
            "en": "what ID number you have on my account",
        },
        "address_label": {
            "pt": "o CEP do endereço de instalação",
            "en": "the ZIP code of the installation address",
        },
        "fillers": {
            "pt": [
                (
                    "Vocês têm plano com mais franquia de dados?",
                    "Temos sim, a oferta {code} dobra a franquia.",
                ),
                (
                    "Como troco a senha do wi-fi?",
                    "Pelo aplicativo, em Rede. Deixei o guia {code} no seu e-mail.",
                ),
                ("Quero a segunda via da conta.", "Enviei a segunda via, código de barras final {code}."),
            ],
            "en": [
                ("Do you have a plan with more data?", "We do, offer {code} doubles the data allowance."),
                (
                    "How do I change the wi-fi password?",
                    "In the app, under Network. I emailed you guide {code}.",
                ),
                ("I need a copy of my bill.", "I sent the copy, reference {code}."),
            ],
        },
    },
    "insurance": {
        "company": {"pt": "Amparo Seguros", "en": "Harbor Insurance"},
        "object_type": "claim",
        "object_ns": "core",
        "item": {"pt": "o sinistro", "en": "claim"},
        "issue": {
            "pt": ["o guincho não apareceu no horário", "a oficina credenciada não tem a peça"],
            "en": ["the tow truck did not show up on time", "the partner garage does not have the part"],
        },
        "issue_about": {"pt": "do guincho atrasado", "en": "about the tow truck being late"},
        "recurring": {
            "pt": [
                "O guincho atrasou de novo, esperei na estrada.",
                "Mais uma vez o guincho não chegou no horário.",
                "Pedi guincho e ele demorou demais outra vez.",
                "De novo o guincho atrasado.",
            ],
            "en": [
                "The tow truck was late again, I waited on the road.",
                "Once more the tow truck did not arrive on time.",
                "I asked for a tow and it took far too long again.",
                "Again the tow truck was late.",
            ],
        },
        "deadline_reason": {
            "pt": ["preciso do carro para trabalhar", "vou viajar com a família"],
            "en": ["I need the car to get to work", "I am traveling with my family"],
        },
        "milestone_type": "inspection.completed",
        "milestone": {"pt": "Vistoria concluída", "en": "Vehicle inspection completed"},
        "milestone_after": {"pt": "da vistoria", "en": "after the inspection"},
        "after_issue": {
            "pt": ["apareceu um barulho na suspensão", "a porta traseira não fecha direito"],
            "en": ["a noise appeared in the suspension", "the rear door does not close properly"],
        },
        "before_issue": {
            "pt": ["o para-choque estava solto", "o farol estava trincado"],
            "en": ["the bumper was loose", "the headlight was cracked"],
        },
        "promise_op": "reimburse",
        "confirm_type": "claim.reimbursed",
        "promise": {
            "pt": "o reembolso de {amount} do táxi",
            "en": "a reimbursement of {amount} for the taxi",
        },
        "promise_noun": {"pt": "reembolso", "en": "reimbursement"},
        "confirm_label": {"pt": "ordem de pagamento", "en": "payment order"},
        "sensitive_kind": "health",
        "sensitive_say": {
            "pt": "Para o seguro de vida: meu diagnóstico é {value}.",
            "en": "For the life policy: my diagnosis is {value}.",
        },
        "sensitive_ask": {
            "pt": "qual diagnóstico está registrado no meu cadastro",
            "en": "what diagnosis is recorded on my file",
        },
        "address_label": {"pt": "o CEP do endereço da apólice", "en": "the ZIP code on my policy"},
        "fillers": {
            "pt": [
                ("Minha apólice cobre vidros?", "Cobre sim, cláusula {code} da apólice."),
                ("Posso incluir um motorista?", "Pode, abri a solicitação {code} para incluir."),
                ("Quando vence o boleto?", "O boleto {code} vence no fim do mês."),
            ],
            "en": [
                ("Does my policy cover glass?", "It does, clause {code} of your policy."),
                ("Can I add a driver?", "Yes, I opened request {code} to add them."),
                ("When is the premium due?", "Invoice {code} is due at the end of the month."),
            ],
        },
    },
    "banking": {
        "company": {"pt": "Banco Horizonte", "en": "Northwind Bank"},
        "object_type": "dispute",
        "object_ns": "core",
        "item": {"pt": "a contestação", "en": "dispute"},
        "issue": {
            "pt": ["meu cartão foi recusado no mercado", "o aplicativo bloqueou minha transferência"],
            "en": ["my card was declined at the grocery store", "the app blocked my transfer"],
        },
        "issue_about": {"pt": "do cartão recusado", "en": "about my card being declined"},
        "recurring": {
            "pt": [
                "Meu cartão foi recusado de novo.",
                "Mais uma vez o cartão não passou na compra.",
                "O cartão voltou a ser recusado hoje.",
                "De novo cartão recusado na loja.",
            ],
            "en": [
                "My card was declined again.",
                "Once more the card did not go through.",
                "The card was declined again today.",
                "Again my card got declined at a store.",
            ],
        },
        "deadline_reason": {
            "pt": ["tenho um aluguel para pagar", "vou viajar e preciso do cartão"],
            "en": ["my rent is due", "I am traveling and need the card"],
        },
        "milestone_type": "card.reissued",
        "milestone": {"pt": "Cartão novo emitido", "en": "Replacement card issued"},
        "milestone_after": {"pt": "da emissão do cartão novo", "en": "after the new card was issued"},
        "after_issue": {
            "pt": ["apareceu uma cobrança que eu não reconheço", "o cartão novo não aceita aproximação"],
            "en": ["a charge I do not recognize appeared", "the new card does not work contactless"],
        },
        "before_issue": {
            "pt": ["o limite foi reduzido sem aviso", "a fatura veio duplicada"],
            "en": ["my limit was lowered without notice", "the statement came twice"],
        },
        "promise_op": "refund_fee",
        "confirm_type": "fee.refunded",
        "promise": {"pt": "o estorno de {amount} da tarifa", "en": "a refund of {amount} for the fee"},
        "promise_noun": {"pt": "estorno", "en": "refund"},
        "confirm_label": {"pt": "comprovante de estorno", "en": "refund receipt"},
        "sensitive_kind": "balance",
        "sensitive_say": {
            "pt": "Meu saldo devedor no cheque especial é {value}.",
            "en": "My overdraft balance is {value}.",
        },
        "sensitive_ask": {"pt": "qual é o meu saldo devedor", "en": "what my overdraft balance is"},
        "address_label": {"pt": "o CEP de entrega do cartão", "en": "the ZIP code for card delivery"},
        "fillers": {
            "pt": [
                ("Qual o horário do Pix noturno?", "O limite noturno vale das 20h às 6h, regra {code}."),
                ("Como aumento meu limite?", "Pelo app, em Cartões. Abri o pedido {code}."),
                ("Vocês têm conta conjunta?", "Temos, o formulário {code} está no site."),
            ],
            "en": [
                (
                    "What are the night transfer limits?",
                    "The night limit runs from 8 pm to 6 am, rule {code}.",
                ),
                ("How do I raise my limit?", "In the app, under Cards. I opened request {code}."),
                ("Do you offer joint accounts?", "We do, form {code} is on the website."),
            ],
        },
    },
    "retail": {
        "company": {"pt": "Loja Aurora", "en": "Maple Home Store"},
        "object_type": "order",
        "object_ns": "erp",
        "item": {"pt": "o pedido", "en": "order"},
        "issue": {
            "pt": ["a panela chegou com a tampa quebrada", "veio a cor errada do sofá"],
            "en": ["the pot arrived with a broken lid", "the sofa came in the wrong color"],
        },
        "issue_about": {"pt": "de item faltando na entrega", "en": "about missing items in a delivery"},
        "recurring": {
            "pt": [
                "Faltou um item na minha entrega.",
                "Mais uma vez a entrega veio incompleta.",
                "De novo veio faltando item no pedido.",
                "A entrega chegou sem um dos produtos outra vez.",
            ],
            "en": [
                "An item was missing from my delivery.",
                "Once more the delivery came incomplete.",
                "Again an item was missing from the order.",
                "The delivery arrived without one of the products again.",
            ],
        },
        "deadline_reason": {
            "pt": ["é presente de aniversário", "vou receber visitas no fim de semana"],
            "en": ["it is a birthday gift", "I am hosting guests this weekend"],
        },
        "milestone_type": "order.delivered",
        "milestone": {"pt": "Entrega concluída", "en": "Delivery completed"},
        "milestone_after": {"pt": "da entrega", "en": "after the delivery"},
        "after_issue": {
            "pt": ["a montagem ficou com um parafuso faltando", "a nota fiscal veio com o endereço errado"],
            "en": ["the assembly was missing a screw", "the invoice came with the wrong address"],
        },
        "before_issue": {
            "pt": ["o rastreio parou de atualizar", "o pagamento ficou pendente"],
            "en": ["tracking stopped updating", "the payment was stuck as pending"],
        },
        "promise_op": "refund",
        "confirm_type": "order.refunded",
        "promise": {"pt": "o reembolso de {amount} do frete", "en": "a refund of {amount} for shipping"},
        "promise_noun": {"pt": "reembolso do frete", "en": "shipping refund"},
        "confirm_label": {"pt": "comprovante de reembolso", "en": "refund receipt"},
        "sensitive_kind": "document",
        "sensitive_say": {
            "pt": "Meu documento para a nota é {value}.",
            "en": "My ID number for the invoice is {value}.",
        },
        "sensitive_ask": {
            "pt": "qual documento vocês têm no meu cadastro",
            "en": "what ID number you have on my account",
        },
        "address_label": {"pt": "o CEP de entrega", "en": "the delivery ZIP code"},
        "fillers": {
            "pt": [
                ("A loja abre no feriado?", "Abre em horário reduzido, aviso {code}."),
                ("Tem cupom para primeira compra?", "Tem, o cupom {code} dá frete grátis."),
                ("Posso trocar o tamanho?", "Pode, a política de troca {code} vale por 30 dias."),
            ],
            "en": [
                ("Is the store open on the holiday?", "Reduced hours, notice {code}."),
                ("Is there a first order coupon?", "Yes, coupon {code} gives free shipping."),
                ("Can I exchange the size?", "Yes, exchange policy {code} applies for 30 days."),
            ],
        },
    },
    "logistics": {
        "company": {"pt": "Rota Express", "en": "Swift Freight"},
        "object_type": "shipment",
        "object_ns": "tms",
        "item": {"pt": "a remessa", "en": "shipment"},
        "issue": {
            "pt": ["o entregador não encontrou o endereço", "a caixa chegou amassada"],
            "en": ["the courier could not find the address", "the box arrived crushed"],
        },
        "issue_about": {"pt": "de entrega não realizada", "en": "about failed delivery attempts"},
        "recurring": {
            "pt": [
                "O entregador não conseguiu entregar de novo.",
                "Mais uma vez a entrega não foi feita.",
                "De novo marcaram tentativa de entrega sem ninguém vir.",
                "A entrega falhou outra vez.",
            ],
            "en": [
                "The courier could not deliver again.",
                "Once more the delivery was not made.",
                "Again they marked a delivery attempt but nobody came.",
                "The delivery failed again.",
            ],
        },
        "deadline_reason": {
            "pt": ["são remédios de uso contínuo", "é material para uma obra"],
            "en": ["it is medication I take daily", "it is material for a construction job"],
        },
        "milestone_type": "pickup.completed",
        "milestone": {"pt": "Coleta realizada", "en": "Pickup completed"},
        "milestone_after": {"pt": "da coleta", "en": "after the pickup"},
        "after_issue": {
            "pt": ["o rastreio mostrou a carga em outra cidade", "o motorista cobrou uma taxa extra"],
            "en": ["tracking showed the load in another city", "the driver charged an extra fee"],
        },
        "before_issue": {
            "pt": ["a etiqueta estava ilegível", "o agendamento foi remarcado sem aviso"],
            "en": ["the label was unreadable", "the appointment was moved without notice"],
        },
        "promise_op": "redeliver",
        "confirm_type": "shipment.rescheduled",
        "promise": {
            "pt": "a nova entrega sem custo e o abatimento de {amount}",
            "en": "a free redelivery and a discount of {amount}",
        },
        "promise_noun": {"pt": "abatimento", "en": "discount"},
        "confirm_label": {"pt": "ordem de reentrega", "en": "redelivery order"},
        "sensitive_kind": "document",
        "sensitive_say": {
            "pt": "O documento de quem vai receber é {value}.",
            "en": "The receiver's ID number is {value}.",
        },
        "sensitive_ask": {
            "pt": "qual documento de recebedor está cadastrado",
            "en": "what receiver ID number is on file",
        },
        "address_label": {"pt": "o CEP de coleta", "en": "the pickup ZIP code"},
        "fillers": {
            "pt": [
                ("Vocês entregam sábado?", "Entregamos, com agendamento {code}."),
                ("Qual o peso máximo por volume?", "Até 30 kg por volume, tabela {code}."),
                ("Como imprimo a etiqueta?", "Pelo portal, a etiqueta {code} já está disponível."),
            ],
            "en": [
                ("Do you deliver on Saturdays?", "We do, with booking {code}."),
                ("What is the maximum weight per box?", "Up to 30 kg per box, table {code}."),
                ("How do I print the label?", "In the portal, label {code} is ready."),
            ],
        },
    },
}

FIRST_NAMES = {
    "pt": [
        "Marina",
        "Rafael",
        "Beatriz",
        "Thiago",
        "Camila",
        "Lucas",
        "Juliana",
        "Pedro",
        "Larissa",
        "Diego",
        "Fernanda",
        "Gustavo",
        "Aline",
        "Bruno",
        "Patrícia",
        "Renato",
        "Carolina",
        "Eduardo",
        "Sabrina",
        "Vitor",
    ],
    "en": [
        "Emily",
        "James",
        "Olivia",
        "Daniel",
        "Sophia",
        "Michael",
        "Grace",
        "Ryan",
        "Hannah",
        "Nathan",
        "Chloe",
        "Ethan",
        "Lauren",
        "Kevin",
        "Megan",
        "Tyler",
        "Rachel",
        "Brandon",
        "Natalie",
        "Sean",
    ],
}
LAST_NAMES = {
    "pt": [
        "Almeida",
        "Barros",
        "Carvalho",
        "Dias",
        "Esteves",
        "Farias",
        "Gomes",
        "Lima",
        "Macedo",
        "Nunes",
        "Oliveira",
        "Pereira",
        "Queiroz",
        "Ramos",
        "Siqueira",
        "Teixeira",
        "Vieira",
        "Xavier",
    ],
    "en": [
        "Anderson",
        "Brooks",
        "Carter",
        "Dawson",
        "Ellis",
        "Foster",
        "Graham",
        "Hayes",
        "Jensen",
        "Keller",
        "Lawson",
        "Morgan",
        "Nolan",
        "Parker",
        "Quinn",
        "Reed",
        "Sullivan",
        "Turner",
    ],
}
CONDITIONS = {
    "pt": ["hérnia de disco", "diabetes tipo 2", "hipertensão arterial", "asma crônica", "enxaqueca crônica"],
    "en": [
        "a herniated disc",
        "type 2 diabetes",
        "high blood pressure",
        "chronic asthma",
        "chronic migraine",
    ],
}
COUNT_WORDS = {
    "pt": {2: ["2", "duas", "dois"], 3: ["3", "tres"], 4: ["4", "quatro"]},
    "en": {2: ["2", "two", "twice"], 3: ["3", "three"], 4: ["4", "four"]},
}
