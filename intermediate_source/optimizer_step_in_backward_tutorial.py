"""

옵티마이저 단계를 backward pass에 합쳐서 메모리 절약하기
======================================================================

**번역**: `하동훈 <https://github.com/hadh93>`_

안녕하세요! 이 튜토리얼에서는 *변화도(gradient)* 가 차지하는 메모리를
줄임으로써 학습 루프(training loop)에서의 메모리 사용량을 줄이는 한 가지
방법을 소개합니다. 모델이 있는 상황에서 메모리 최적화를 통해 메모리 
부족(Out of Memory, OOM) 오류를 방지하고 싶거나, GPU의 성능을 최대한 
활용하고 싶은 경우 이 방법이 도움이 될 수 있습니다 (변화도가 메모리의 
일부분을 차지하고 있으며, 변화도 누적(accumulation)이 필요하지 
않은 경우라면 말입니다). 이 튜토리얼은 다음 내용을 다룹니다:

1. 학습 또는 미세 조정(finetuning) 루프에서 메모리를 차지하는 요소,
2. 병목 현상을 파악하기 위해 메모리 스냅샷(snapshot)을 캡처하고 시각화하는 방법,
3. 새로운 ``Tensor.register_post_accumulate_grad_hook(hook)`` API,
4. 이 모든 것을 감안한 단 10줄의 코드로 메모리를 절약하는 방법.

이 튜토리얼을 실행하기 위해 필요한 것:

*  2.1.0 혹은 그 이상의 버전의 PyTorch와 ``torchvision``
*  메모리 시각화를 로컬에서 실행하려면, CUDA GPU 1개.
   메모리 시각화를 제외하면 이 최적화 방법은 모든 장치에서 유사한 이점을 제공합니다.

먼저 필요한 모듈과 모델을 import 하겠습니다. 여기에서는 torchvision의 비전 
트랜스포머 모델을 사용하지만, 다른 모델로 대체해도 좋습니다. 또 옵티마이저로 
``torch.optim.Adam`` 을 사용하지만, 마찬가지로 다른 옵티마이저로 대체해도 
됩니다.

"""

import torch
from torchvision import models
from pickle import dump

model = models.vit_l_16(weights='DEFAULT').cuda()
optimizer = torch.optim.Adam(model.parameters())

###############################################################################
# 이제 일반적인 학습 루프를 정의해봅시다. 실제 학습 시에는 진짜 이미지를 사용해야 
# 하지만, 이 튜토리얼에서는 가짜 입력 데이터를 사용하며 
# 실제 데이터를 읽어들이는 것에 대해서는 신경 쓰지 않을 것입니다.

IMAGE_SIZE = 224

def train(model, optimizer):
  # 가짜 이미지 입력값 생성: tensor의 형태는 batch_size, channels, height, width
  fake_image = torch.rand(1, 3, IMAGE_SIZE, IMAGE_SIZE).cuda()

  # forward와 backward 호출
  loss = model.forward(fake_image)
  loss.sum().backward()

  # 옵티마이저 업데이트
  optimizer.step()
  optimizer.zero_grad()

###############################################################################
# 학습 중의 메모리 사용량
# """"""""""""""""""""""""""""
# 이제 메모리 스냅샷을 확인하려고 하므로, 이를 적절히 분석할 준비를 해야 합니다.
# 일반적으로 학습 메모리는 다음으로 구성됩니다:
#
#  * 모델 매개변수 (크기 P)
#  * backward pass를 위해 저장된 활성화 값(activations) (크기 A)
#  * 변화도, 모델 매개변수와 같은 크기이므로 크기 G = P.
#  * 옵티마이저 상태, 매개변수 크기에 비례합니다. 예시의 경우, 
#    Adam의 상태는 모델 매개변수의 2배가 필요하므로 크기 O = 2P.
#  * 중간 단계(Intermediate) tensor, 계산 도중 할당됩니다. 
#    보통 크기가 작고 일시적이므로 지금은 신경 쓰지 않겠습니다.
#
# 메모리 스냅샷 캡처 및 시각화
# """"""""""""""""""""""""""""""""""""""""""
# 이제 메모리 스냅샷을 가져와 봅시다! 코드가 실행되는 동안,
# CUDA 메모리 타임라인이 어떤 모습일지 한 번 예상해 보세요.

# CUDA에 메모리 할당 기록을 시작하도록 지시
torch.cuda.memory._record_memory_history(enabled='all')

# 학습 3회 실시
for _ in range(3):
  train(model, optimizer)

# 메모리 할당 스냅샷을 저장
s = torch.cuda.memory._snapshot()
with open(f"snapshot.pickle", "wb") as f:
    dump(s, f)

# CUDA에 메모리 할당 기록을 중지하도록 지시
torch.cuda.memory._record_memory_history(enabled=None)

###############################################################################
# 이제 CUDA 메모리 시각화 도구(CUDA Memory Visualizer)에서 스냅샷을 열어보세요.
# https://pytorch.org/memory_viz 로 들어가서 ``snapshot.pickle`` 파일을 드래그 앤
# 드롭하여 업로드할 수 있습니다. 메모리 타임라인이 예상과 일치하나요?
# 
# .. figure:: /_static/img/optim_step_in_bwd/snapshot.jpg
#    :alt: snapshot.png loaded into CUDA Memory Visualizer
# 
# 모델 매개변수는 이미 학습 루프 이전에 메모리에 로드되었으므로,
# 처음부터 가중치(weights)에 할당된 메모리 덩어리가 보입니다.
# forward pass를 시작하면, 메모리는 활성화 값을 위해 점차 할당됩니다.
# 이 활성화 값은 backward pass에서 변화도를 계산하기 위해 저장하는 tensor입니다.
# backward pass를 시작하면, 활성화 값이 점차 해제되면서 변화도가 차지하는 메모리가
# 쌓이기 시작합니다.
# 
# 마지막으로 옵티마이저가 작동하면, 옵티마이저의 상태는 지연(lazily) 초기화되므로,
# 첫 번째 학습 루프의 옵티마이저 단계 동안만 옵티마이저 상태 메모리가 점차 
# 증가하는 것을 볼 수 있습니다. 이후의 루프에서는, 옵티마이저 메모리가 그대로 
# 유지되고, 제자리에서 업데이트됩니다. 변화도가 차지하는 메모리는 매번 학습 루프가
# 끝날 때에 맞춰 ``zero_grad`` 가 호출되면 해제됩니다.
# 
# 이 학습 루프에서 메모리 병목 현상이 발생하는 지점은 어디일까요? 즉, 메모리 
# 사용이 가장 높은 지점은 어디일까요?
# 
# 메모리 사용량이 가장 높은 지점은 옵티마이저 단계입니다! 이때의 메모리를 보면 예상대로
# ~1.2GB 의 매개변수, ~1.2GB의 변화도, 그리고 ~2.4GB=2*1.2GB 의 옵티마이저 상태로
# 구성됩니다. 마지막 ~1.2GB는 Adam 옵티마이저가 중간 단계에 필요로 하는 메모리로,
# 합쳐서 총 ~6GB에 달합니다.
# 사실, ``Adam(model.parameters(), foreach=False)`` 로 설정하면 옵티마이저 중간
# 메모리인 마지막 1.2GB를 제거할 수 있는데, 이는 메모리 대신 실행 시간을 희생하는 
# 방식입니다. 만약 이 ``foreach`` 최적화만으로도 충분히 필요한만큼 메모리가 
# 절약되었다면 잘된 일이지만, 
# 더 나은 방법에 대해 알고 싶다면 이 튜토리얼을 계속 읽어보세요!
# 이제 곧 소개할 방법을 사용한다면, ~1.2GB의 **변화도 메모리** 와 **옵티마이저 중간 
# 단계 메모리** 가 필요 없게 되어 최대 메모리 사용량을 낮출 수 있습니다.
# 그렇다면, 새로운 최대 메모리 사용량은 얼마가 될까요?
# 정답은 `다음` 스냅샷에서 공개됩니다.
#
# 주의 사항: 이 방법은 모든 경우에 적합한 것은 **아님**
# """""""""""""""""""""""""""""""""""""""""""""
# 잠시 진정하고, 먼저 이 방법이 `당신` 의 사용 사례에 적합한지 고려해야 합니다.
# 이 방법은 결코 만능 해결책이 아닙니다! 
# 옵티마이저 단계를 backward 과정에 합치는 이 방법은 *변화도* 메모리의 감소만을 목표로 
# 합니다 (그리고 부수적으로 옵티마이저 중간 단계 메모리도 줄입니다). 
# 따라서 변화도가 차지하는 메모리가 클수록, 메모리 절약 효과가 더욱 커집니다. 
# 위의 예시에서 변화도는 메모리 총량의 20%를 차지하는데, 이는 꽤나 큰 비율이죠!
#
# 그러나 때에 따라 이러한 상황에 해당하지 않을 수 있습니다. 예를 들어, 이미 
# 가중치가 매우 작다면 (LoRa 적용 등의 이유로), 변화도가 학습 루프에서 공간을 많이 
# 차지하지 않을 것이고, 그렇다면 이 방법의 이점이 그다지 크지 않을 수 있습니다.
# 이런 경우에는 먼저 활성화 값 체크포인팅, 분산 학습, 양자화, 배치 크기 축소와 같은 
# 다른 기술을 시도해 보세요. 그런 다음, 변화도가 다시 병목의 일부가 될 때 
# 이 튜토리얼로 돌아오세요!
# 
# 아직 여기에 계신가요? 좋습니다, 이제 Tensor의 새로운 ``register_post_accumulate_grad_hook(hook)``
# API를 소개하겠습니다.
#
# ``Tensor.register_post_accumulate_grad_hook(hook)`` API와 우리가 사용할 방법
# """"""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# 이 방법은 ``backward()`` 동안 변화도를 저장하지 않아도 된다는 점에 의존합니다. 
# 대신, 기울기가 누적되면 즉시 해당 매개변수에 대해 옵티마이저를 적용하고, 해당 
# 변화도를 완전히 제거합니다! 이렇게 하면 옵티마이저 단계를 위해 큰 변화도 버퍼를 
# 유지할 필요가 없어집니다.
#
# 그렇다면 옵티마이저를 더 즉시(eagerly) 적용하는 동작을 어떻게 하면 활성화할 수 있을까요? 2.1 
# 버전에서 새로 추가된 API인 :func:`torch.Tensor.register_post_accumulate_grad_hook`
# 을 사용하면, Tensor의 ``.grad``  필드(field)가 누적된 후에 훅(hook)을 추가할 수 있습니다.
# 우리는 이 훅에 옵티마이저 단계를 캡슐화(encapsulate)할 것입니다. 어떻게요?
# 
# 이 모든 것을 단 10줄의 코드로 통합하는 방법
# """"""""""""""""""""""""""""""""""""""""
# 초반에 사용했던 모델과 옵티마이저 설정을 기억하시나요? 코드 재실행에 리소스를 
# 낭비하지 않도록 아래에 주석으로 남겨두겠습니다.
#
# .. code-block:: python
#
#    model = models.vit_l_16(weights='DEFAULT').cuda()
#    optimizer = torch.optim.Adam(model.parameters())

# *단일* 옵티마이저 대신, 각 매개변수마다 하나씩 옵티마이저를 만들고 ``딕셔너리``
# 하나에 저장하여 훅에서 참조할 수 있도록 하겠습니다.
optimizer_dict = {p: torch.optim.Adam([p], foreach=False) for p in model.parameters()}

# 옵티마이저의 ``step()`` 및 ``zero_grad()`` 를 호출할 훅을 정의합니다.
def optimizer_hook(parameter) -> None:
  optimizer_dict[parameter].step()
  optimizer_dict[parameter].zero_grad()

# 모든 매개변수에 훅을 등록합니다.
for p in model.parameters():
   p.register_post_accumulate_grad_hook(optimizer_hook)

# 이전의 ``train()`` 함수 기억하시나요? 옵티마이저가 backward에 합쳐졌으므로, 
# 옵티마이저의 step 및 zero_grad 호출을 제거할 수 있습니다.
def train(model):
  # 가짜 이미지 입력값 생성: tensor의 형태는 batch_size, channels, height, width
  fake_image = torch.rand(1, 3, IMAGE_SIZE, IMAGE_SIZE).cuda()

  # forward와 backward 호출
  loss = model.forward(fake_image)
  loss.sum().backward()

  # 옵티마이저 업데이트 --> 이제 필요 없습니다!
  # optimizer.step()
  # optimizer.zero_grad()

########################################################################
# 샘플 모델에서는 약 10줄의 코드 변경으로 끝났습니다. 깔끔하네요.
# 하지만 실제 모델에서는 옵티마이저를 옵티마이저 딕셔너리로 교체하는 것이 
# 꽤나 거슬리는 변경이 될 수 있습니다. 
# 특히 ``LRScheduler`` 를 사용하거나 학습 에폭 동안 옵티마이저 구성을 
# 조작하는 경우에는 더욱 그렇습니다. 
# 그러한 상황에서 이 API를 사용하려면 더 복잡할 것이고, 더 많은 구성 요소를 
# 전역(global) 상태로 이동시켜야 할 수도 있지만, 불가능하지는 않을 것입니다.
# 그렇긴 하지만, 조만간 PyTorch가 이 API를 LRScheduler나 기존의 다른
# 기능들과 더 쉽게 통합할 수 있도록 이 API를 개선하길 바라 봅니다.
# 
# 다시 돌아와서, 이 방법이 써볼 만한 가치가 있다는 설득을 이어 나가 보겠습니다.
# 우리의 친구, 메모리 스냅샷을 살펴보겠습니다.

# 이전의 옵티마이저 메모리를 삭제하여 다음 메모리 스냅샷을 위한 깨끗한 상태를 
# 만듭니다.
del optimizer

# CUDA에 메모리 할당 기록을 시작하도록 지시
torch.cuda.memory._record_memory_history(enabled='all')

# 학습 3회 실시. 이제 더 이상 train() 함수에 옵티마이저를 전달하지 않는다는 점에 유의하세요. 
for _ in range(3):
  train(model)

# 메모리 할당 스냅샷을 저장
s = torch.cuda.memory._snapshot()
with open(f"snapshot-opt-in-bwd.pickle", "wb") as f:
    dump(s, f)

# CUDA에 메모리 할당 기록을 중지하도록 지시
torch.cuda.memory._record_memory_history(enabled=None)

###############################################################################
# 좋아요, CUDA Memory Visualizer에 스냅샷을 드래그 앤 드롭해 봅시다.
# 
# .. figure:: /_static/img/optim_step_in_bwd/snapshot_opt_in_bwd.jpg
#    :alt: snapshot.png loaded into CUDA Memory Visualizer
#
# 몇 가지 주요 관찰 사항:
#  1. 더 이상 옵티마이저 단계가 없습니다! 맞아요... backward 과정에 합쳐졌죠.
#  2. 마찬가지로, backward 과정이 더 길어지고 중간 단계를 위한 임시 메모리 할당이 더 
#     많아졌습니다. 이는 예상된 결과인데, 옵티마이저 단계가 중간 단계를 필요로 하기 
#     때문입니다.
#  3. 가장 중요한 점! 최대 메모리 사용량이 낮아졌습니다! 이제 ~4GB 정도입니다 
#     (예상하셨던 수치와 얼추 비슷하길 바랍니다). 
# 
# 더 이상 변화도를 위해 할당된 큰 메모리 덩어리가 없다는 점에 주목하세요.
# 이에 따라 이전과 비교해 보았을 때 ~1.2GB 의 메모리 절약이 이루어졌습니다. 대신, 
# 변화도가 계산되자마자 매우 빠르게 해제되었는데, 이는 가능한 한 옵티마이저 단계를 
# 앞당겼기 때문입니다. 야호! 참고로, 나머지 ~1.2GB 의 메모리 절약은 옵티마이저를 
# 매개변수별 옵티마이저로 나누면서 중간 단계의 메모리 사용량이 줄어든 덕분입니다. 
# 하지만 이것은 기울기 메모리 절약보다는 `덜 중요한` 부분인데, 왜냐하면 중간 단계의 
# 메모리 절약은 이 기술 없이도 ``foreach=False`` 옵션을 수정해 주는 것만으로 
# 달성할 수 있기 때문입니다.
# 
# 이런 의문이 생길 수 있습니다: 2.4GB의 메모리가 절약되었다면, 왜 최대 메모리 사용량이 
# 6GB - 2.4GB = 3.6GB가 아닌가요? 아, 그건 최대 메모리 사용량의 시점이 바뀌었기 때문입니다!
# 최대 메모리 사용량은 이제 backward 과정의 시작 부분으로 이동했습니다. 이제는 이 시점에 
# 메모리에 활성화 값이 남아있으며, 이전에는 옵티마이저 단계에서 활성화 값이 해제된 후 최대 
# 메모리 사용량이 발생했습니다. ~4.0GB와 ~3.6GB 간의 ~0.4GB의 차이는 바로 이 활성화 값의 
# 메모리 때문입니다. 따라서 이 기술을 활성화 값 체크포인팅과 함께 사용하면 더 큰 메모리 
# 절약을 이룰 수 있을 것입니다.
#
# 결론
# """"""""""
# 이번 튜토리얼에서는 새로운 ``Tensor.register_post_accumulate_grad_hook()`` 
# API를 사용하여 옵티마이저를 backward 단계에 합치는 메모리 절약 기술과 이를 *언제* 
# 적용해야 하는지(변화도 메모리 양이 상당한 경우)에 대해 배웠습니다. 또한 메모리 
# 최적화에 일반적으로 유용한 메모리 스냅샷에 대해서도 학습했습니다.
# 
