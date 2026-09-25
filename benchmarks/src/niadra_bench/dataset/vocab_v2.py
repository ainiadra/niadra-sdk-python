"""Vocabulary the dataset v2 categories add to each domain of `vocab.py`, in Portuguese and English.

Written for a customer, not for a memory system: nothing here names a field, a category or a word any
system under test matches on. Every number is still drawn by the generator.

- `paraphrases`: a fact the customer states and a question about it that shares no content word with
  the statement (the structural rule checks it), so only the meaning links the two.
- `recurring_other`: a second recurring complaint of the domain, a different matter from `recurring`,
  for the count-by-topic cases.
- `more_fillers`: more everyday exchanges for customers with 30 to 60 sessions; some hand out a
  protocol number, so in a long history the kind of value alone never identifies the answer.
"""

from __future__ import annotations

from typing import Literal, TypedDict

ValueKind = Literal["code", "amount"]


class Paraphrase(TypedDict):
    kind: ValueKind
    statement: str
    question: str
    answer: str


class DomainV2(TypedDict):
    paraphrases: dict[str, list[Paraphrase]]
    recurring_other: dict[str, list[str]]
    more_fillers: dict[str, list[tuple[str, str]]]


def _p(kind: ValueKind, statement: str, question: str, answer: str) -> Paraphrase:
    return {"kind": kind, "statement": statement, "question": question, "answer": answer}


V2: dict[str, DomainV2] = {
    "telecom": {
        "paraphrases": {
            "pt": [
                _p(
                    "code",
                    "Pode anotar: o aparelho que dá problema é o decodificador da sala, número de série {v}.",
                    "Qual é a identificação do equipamento com defeito que eu informei?",
                    "O número de série é {v}.",
                ),
                _p(
                    "code",
                    "Quando o técnico vier, a portaria libera com o código {v}.",
                    "Que senha o porteiro precisa para deixar o instalador subir?",
                    "O código da portaria é {v}.",
                ),
            ],
            "en": [
                _p(
                    "code",
                    "Please note: the faulty unit is the living room set-top box, serial {v}.",
                    "Which identifier did I give for the broken device?",
                    "The serial is {v}.",
                ),
                _p(
                    "code",
                    "When the technician comes, the front desk lets him in with access code {v}.",
                    "What passcode does the doorman need so the installer can go up?",
                    "The access code is {v}.",
                ),
            ],
        },
        "recurring_other": {
            "pt": [
                "Veio de novo uma cobrança errada na minha fatura.",
                "Mais uma vez cobraram um serviço que eu não contratei.",
                "A fatura chegou com valor a mais outra vez.",
                "De novo uma cobrança indevida na conta.",
            ],
            "en": [
                "My bill came with a wrong charge again.",
                "Once more I was charged for a service I never ordered.",
                "The bill arrived with an extra amount again.",
                "Again an improper charge on my account.",
            ],
        },
        "more_fillers": {
            "pt": [
                ("Quero mudar a data de vencimento.", "Mudei para o dia que você pediu, protocolo {code}."),
                ("Vocês têm fibra no meu bairro?", "Temos, a consulta de cobertura {code} confirmou."),
                ("Como desbloqueio o chip?", "Mandei o código de desbloqueio pelo app, pedido {code}."),
                (
                    "Quero cancelar o pacote de canais.",
                    "Cancelado a partir do próximo mês, protocolo {code}.",
                ),
                ("O app mostra meu consumo?", "Mostra, na aba Consumo. Deixei o guia {code} no e-mail."),
                ("Posso levar meu número para outra linha?", "Pode, abri a portabilidade {code}."),
            ],
            "en": [
                ("I want to change my due date.", "I moved it to the day you asked, protocol {code}."),
                ("Do you have fiber in my area?", "We do, coverage check {code} confirmed it."),
                ("How do I unlock my SIM?", "I sent the unlock code in the app, request {code}."),
                ("I want to cancel the TV channel pack.", "Cancelled from next month, protocol {code}."),
                ("Does the app show my usage?", "It does, under Usage. I emailed you guide {code}."),
                ("Can I move my number to another line?", "Yes, I opened number transfer {code}."),
            ],
        },
    },
    "insurance": {
        "paraphrases": {
            "pt": [
                _p(
                    "amount",
                    "O orçamento do conserto deu {v}.",
                    "Qual valor eu disse que a oficina pediu para arrumar o veículo?",
                    "O orçamento foi de {v}.",
                ),
                _p(
                    "code",
                    "Peguei o carro reserva, contrato da locadora {v}.",
                    "Qual é o número do aluguel do automóvel substituto que eu mencionei?",
                    "O contrato é {v}.",
                ),
            ],
            "en": [
                _p(
                    "amount",
                    "The repair estimate came to {v}.",
                    "What price did I say the garage quoted to fix the vehicle?",
                    "The estimate was {v}.",
                ),
                _p(
                    "code",
                    "I picked up the courtesy car, rental agreement {v}.",
                    "Which number did I mention for the substitute automobile's hire?",
                    "The agreement is {v}.",
                ),
            ],
        },
        "recurring_other": {
            "pt": [
                "A oficina atrasou o conserto de novo.",
                "Mais uma vez a oficina não entregou o carro no prazo.",
                "O reparo voltou a atrasar na oficina.",
                "De novo a oficina adiou a entrega do carro.",
            ],
            "en": [
                "The garage delayed the repair again.",
                "Once more the garage missed the date to return my car.",
                "The repair got delayed at the garage again.",
                "Again the garage pushed back returning my car.",
            ],
        },
        "more_fillers": {
            "pt": [
                ("Quero a segunda via da apólice.", "Enviei a segunda via, protocolo {code}."),
                ("A apólice cobre carro reserva?", "Cobre por sete dias, cláusula {code}."),
                ("Posso pagar em mais parcelas?", "Pode, simulei o parcelamento {code}."),
                ("Mudei de cidade, preciso avisar?", "Precisa, abri a atualização cadastral {code}."),
                ("Como aciono a assistência 24 horas?", "Pelo app ou telefone, guia {code}."),
                ("Quero incluir cobertura de vidros.", "Incluí na renovação, protocolo {code}."),
            ],
            "en": [
                ("I need a copy of my policy.", "I sent the copy, protocol {code}."),
                ("Does the policy cover a courtesy car?", "For seven days, clause {code}."),
                ("Can I pay in more installments?", "Yes, I ran installment plan {code}."),
                ("I moved to another city, do I need to tell you?", "Yes, I opened profile update {code}."),
                ("How do I call roadside assistance?", "In the app or by phone, guide {code}."),
                ("I want to add glass cover.", "Added at renewal, protocol {code}."),
            ],
        },
    },
    "banking": {
        "paraphrases": {
            "pt": [
                _p(
                    "amount",
                    "A compra que eu não reconheço foi de {v}, num posto de gasolina.",
                    "De quanto era o gasto desconhecido que eu mencionei?",
                    "O valor é {v}.",
                ),
                _p(
                    "code",
                    "O boleto que paguei em duplicidade tem final {v}.",
                    "Qual a terminação da conta de cobrança quitada em dobro que eu citei?",
                    "O boleto termina em {v}.",
                ),
            ],
            "en": [
                _p(
                    "amount",
                    "The purchase I don't recognize was {v}, at a gas station.",
                    "How much was the unknown expense I mentioned?",
                    "It was {v}.",
                ),
                _p(
                    "code",
                    "The invoice I paid by mistake a second time ends in {v}.",
                    "Which payment slip got settled in duplicate, by its last digits?",
                    "It ends in {v}.",
                ),
            ],
        },
        "recurring_other": {
            "pt": [
                "O aplicativo travou de novo na hora de pagar.",
                "Mais uma vez o app ficou fora do ar.",
                "O aplicativo voltou a fechar sozinho.",
                "De novo não consegui entrar no aplicativo.",
            ],
            "en": [
                "The app froze again when I tried to pay.",
                "Once more the app was down.",
                "The app kept closing by itself again.",
                "Again I could not log in to the app.",
            ],
        },
        "more_fillers": {
            "pt": [
                ("Quero o informe de rendimentos.", "Enviei o informe pelo app, protocolo {code}."),
                ("Como cadastro uma chave Pix?", "No app, em Pix. Deixei o guia {code} no e-mail."),
                ("Quero aumentar o limite do Pix.", "Pedido registrado, protocolo {code}."),
                ("Vocês têm cartão sem anuidade?", "Temos, a oferta {code} está no app."),
                ("Como pago a fatura com saldo?", "Em Cartões, Pagar fatura. Guia {code}."),
                ("Quero encerrar a conta poupança.", "Abri o encerramento, protocolo {code}."),
            ],
            "en": [
                ("I need my annual tax statement.", "I sent it in the app, protocol {code}."),
                ("How do I register an instant payment key?", "In the app, under Payments. Guide {code}."),
                ("I want a higher instant payment limit.", "Request logged, protocol {code}."),
                ("Do you have a card with no annual fee?", "We do, offer {code} is in the app."),
                ("How do I pay the card with my balance?", "Under Cards, Pay bill. Guide {code}."),
                ("I want to close my savings account.", "I opened the closure, protocol {code}."),
            ],
        },
    },
    "retail": {
        "paraphrases": {
            "pt": [
                _p(
                    "code",
                    "O código que aparece na etiqueta da caixa é {v}.",
                    "Que número estava impresso no rótulo da embalagem, segundo eu falei?",
                    "O código é {v}.",
                ),
                _p(
                    "amount",
                    "Paguei {v} pela montagem do guarda-roupa.",
                    "Quanto custou instalar o armário, pelo que eu contei?",
                    "Custou {v}.",
                ),
            ],
            "en": [
                _p(
                    "code",
                    "The code on the box label is {v}.",
                    "What number was printed on the package sticker, as I told you?",
                    "The code is {v}.",
                ),
                _p(
                    "amount",
                    "I paid {v} for the wardrobe assembly.",
                    "How much did putting the closet together cost, from what I said?",
                    "It cost {v}.",
                ),
            ],
        },
        "recurring_other": {
            "pt": [
                "O produto chegou com defeito de novo.",
                "Mais uma vez veio uma peça quebrada.",
                "Outra vez recebi um item com defeito.",
                "De novo o produto veio danificado.",
            ],
            "en": [
                "The product arrived defective again.",
                "Once more a part came broken.",
                "Yet again I got a defective item.",
                "Again the product came damaged.",
            ],
        },
        "more_fillers": {
            "pt": [
                ("Quero a nota fiscal do último pedido.", "Enviei a nota por e-mail, protocolo {code}."),
                ("Vocês entregam no mesmo dia?", "Em algumas regiões, tabela {code}."),
                ("Posso retirar na loja?", "Pode, a retirada {code} fica pronta hoje."),
                ("Quero cancelar uma compra.", "Cancelei e abri o estorno, protocolo {code}."),
                ("Tem garantia estendida?", "Tem, o plano {code} acrescenta um ano."),
                ("Como avalio um produto?", "Pelo app, em Meus pedidos. Guia {code}."),
            ],
            "en": [
                ("I need the invoice for my last order.", "I emailed it, protocol {code}."),
                ("Do you deliver the same day?", "In some areas, table {code}."),
                ("Can I pick up in store?", "Yes, pickup {code} is ready today."),
                ("I want to cancel a purchase.", "Cancelled and refund opened, protocol {code}."),
                ("Is there an extended warranty?", "Yes, plan {code} adds a year."),
                ("How do I review a product?", "In the app, under My orders. Guide {code}."),
            ],
        },
    },
    "logistics": {
        "paraphrases": {
            "pt": [
                _p(
                    "code",
                    "O lacre do contêiner tem a numeração {v}.",
                    "Qual identificação do selo de segurança da carga eu passei?",
                    "O lacre é {v}.",
                ),
                _p(
                    "amount",
                    "A nota do frete que eu paguei foi de {v}.",
                    "Quanto custou o transporte, de acordo com o que eu contei?",
                    "Custou {v}.",
                ),
            ],
            "en": [
                _p(
                    "code",
                    "The container seal is numbered {v}.",
                    "Which security tag identifier for the load did I give you?",
                    "The seal is {v}.",
                ),
                _p(
                    "amount",
                    "The freight bill I paid was {v}.",
                    "How much did the transport cost, according to what I told you?",
                    "It cost {v}.",
                ),
            ],
        },
        "recurring_other": {
            "pt": [
                "A caixa chegou amassada de novo.",
                "Mais uma vez a embalagem veio danificada.",
                "Outra vez a carga chegou avariada.",
                "De novo recebi a caixa rasgada.",
            ],
            "en": [
                "The box arrived crushed again.",
                "Once more the package came damaged.",
                "Yet again the load arrived damaged.",
                "Again I got the box torn.",
            ],
        },
        "more_fillers": {
            "pt": [
                ("Quero o comprovante de entrega.", "Enviei o comprovante, protocolo {code}."),
                ("Vocês fazem coleta reversa?", "Fazemos, a coleta {code} está agendada."),
                ("Qual o prazo para o interior?", "Até uma semana, tabela {code}."),
                ("Quero mudar o endereço de entrega.", "Alterei a rota, protocolo {code}."),
                ("Vocês têm seguro de carga?", "Temos, a apólice {code} cobre o valor declarado."),
                ("Como rastreio minha remessa?", "Pelo portal, com o código {code}."),
            ],
            "en": [
                ("I need the proof of delivery.", "I sent it, protocol {code}."),
                ("Do you do return pickups?", "We do, pickup {code} is booked."),
                ("How long to rural areas?", "Up to a week, table {code}."),
                ("I want to change the delivery address.", "Route changed, protocol {code}."),
                ("Do you have cargo insurance?", "We do, policy {code} covers the declared value."),
                ("How do I track my shipment?", "In the portal, with code {code}."),
            ],
        },
    },
}
